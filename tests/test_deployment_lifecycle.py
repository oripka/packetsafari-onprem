import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from packetsafari_onprem import operations as ops, maintenance_update as maintenance
from packetsafari_onprem import fleet_update as fleet
from packetsafari_onprem.rolling_update import Runtime, save, read


@pytest.mark.parametrize('mode', ['fleet', 'maintenance'])
def test_pending_update_reverifies_saved_signature_without_fetching_new_channel(tmp_path, monkeypatch, mode):
    layout = ops.runtime_layout(str(tmp_path), '/storage/onprem')
    directory = layout.state_dir / 'rolling'
    directory.mkdir(parents=True)
    save(directory / 'transaction.json', {'mode': mode, 'phase': 'draining'})
    manifest = directory / 'pinned-release.json'
    original = b'{ "version": "saved-release" }\n'
    manifest.write_bytes(original)
    private = tmp_path / 'test-private.pem'
    public = tmp_path / 'test-public.pem'
    subprocess.run(['openssl', 'genrsa', '-out', str(private), '2048'], check=True, capture_output=True)
    subprocess.run(['openssl', 'rsa', '-in', str(private), '-pubout', '-out', str(public)], check=True, capture_output=True)
    subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(private), '-out', str(manifest)+'.sig', str(manifest)], check=True)
    monkeypatch.setattr(ops, '_resolve_release_public_key', lambda _: public)
    monkeypatch.setattr(ops, '_update_manifest_source', lambda *a: pytest.fail('must not fetch channel while pending'))
    args = SimpleNamespace(manifest_url='https://channel/newer.json', human_output=False)
    fetched = ops._download_update_manifest(args, layout)
    assert fetched.read_bytes() == original
    assert args._release_signature_verified is True
    manifest.write_bytes(b'{"version":"tampered"}')
    with pytest.raises(RuntimeError, match='signature verification failed'):
        ops._download_update_manifest(args, layout)


def test_pinning_preserves_exact_signed_bytes(tmp_path):
    directory = tmp_path / 'rolling'
    directory.mkdir()
    source = tmp_path / 'target.json'
    source.write_bytes(b'{  "version" : "one" }\n')
    Path(str(source)+'.sig').write_bytes(b'opaque-signature')
    fleet.pin_release(SimpleNamespace(directory=directory), source)
    assert (directory/'pinned-release.json').read_bytes() == source.read_bytes()
    assert (directory/'pinned-release.json.sig').read_bytes() == b'opaque-signature'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path, tmp_path/'compose.json', ['docker', 'compose'])
    base = {'services': {service: {'image': 'old', 'container_name': service} for service in fleet.COHORT}}
    save(runtime.stack_file, {'active': 'backend', 'proxyName': 'proxy', 'proxyImage': 'nginx',
                             'fleetBases': {'backend': base, 'backend-green': base}})
    save(runtime.compose_file, base)
    events = []
    monkeypatch.setattr(runtime, 'dc', lambda *args: events.append(args) or '')
    monkeypatch.setattr(runtime, 'container', lambda service: service)
    monkeypatch.setattr(runtime, 'render', lambda stack: events.append(('render',)))
    monkeypatch.setattr(maintenance.proxy, 'quiesce', lambda *args: {'status': 'drained'})
    monkeypatch.setattr(maintenance.proxy, 'switch', lambda *args, **kwargs: events.append(('switch',)))
    monkeypatch.setattr(maintenance.proxy, 'docker', lambda *args: events.append(args))
    monkeypatch.setattr(maintenance, 'ready', lambda *args: events.append(('ready',)))
    monkeypatch.setattr(maintenance.workload_drain, 'begin_worker', lambda worker: {'status': 'prepared'})
    def request(receipt, persist):
        receipt = {**receipt, 'status': 'requested'}
        persist(receipt)
        return receipt
    monkeypatch.setattr(maintenance.workload_drain, 'request_worker', request)
    monkeypatch.setattr(maintenance.workload_drain, 'wait_worker', lambda *args: {'status': 'drained'})
    return runtime, base, events


def test_maintenance_timeout_retains_dependencies_and_does_not_prepare(runtime, monkeypatch):
    instance, base, events = runtime
    monkeypatch.setattr(maintenance.workload_drain, 'wait_worker', lambda *a: {'status': 'draining'})
    outcome = maintenance.deploy(instance, prepare=lambda _: pytest.fail('cannot prepare before drain'), start=lambda: None, target=base)
    assert outcome['status'] == 'draining'
    assert not any(event[0] in ('stop', 'render') for event in events)
    assert read(instance.journal_file)['phase'] == 'draining'


def test_interrupted_maintenance_reuses_plan_and_does_not_redo_prepare(runtime):
    instance, base, events = runtime
    prepared, started, committed = [], [], []
    def prepare(target):
        assert any(event[0] == 'stop' for event in events)
        prepared.append(target)
        return target
    def start():
        started.append(True)
        if len(started) == 1:
            raise RuntimeError('interrupted startup')
    kwargs = dict(prepare=prepare, start=start, target=base, commit=lambda: committed.append(True))
    with pytest.raises(RuntimeError, match='interrupted startup'):
        maintenance.deploy(instance, **kwargs)
    assert read(instance.journal_file)['phase'] == 'starting'
    assert maintenance.deploy(instance, **kwargs)['status'] == 'ok'
    assert len(prepared) == 1 and len(started) == 2 and committed == [True]
    assert not instance.journal_file.exists()


def test_failed_maintenance_verification_keeps_journal_without_database_rollback(runtime, monkeypatch):
    instance, base, events = runtime
    pauses = []
    monkeypatch.setattr(maintenance.proxy, 'quiesce', lambda *a: pauses.append(True) or {'status': 'drained'})
    def fail():
        raise RuntimeError('doctor failed')
    with pytest.raises(RuntimeError, match='doctor failed'):
        maintenance.deploy(instance, prepare=lambda target: target, start=lambda: None,
                           verify=fail, target=base, commit=lambda: pytest.fail('must not commit'))
    assert len(pauses) == 2
    assert read(instance.journal_file)['phase'] == 'opening'
    with pytest.raises(RuntimeError, match='may have changed schema'):
        instance.recover()


def test_abort_refuses_a_partially_committed_release(tmp_path):
    instance = Runtime(tmp_path, tmp_path/'compose.json', [])
    save(instance.journal_file, {'mode': 'fleet', 'phase': 'committing'})
    with pytest.raises(RuntimeError, match='commit has begun'):
        fleet.abort(instance)
    assert read(instance.journal_file)['phase'] == 'committing'


@pytest.mark.parametrize('restarted', [False, True])
def test_worker_readiness_resume_preserves_candidate_identity(tmp_path, monkeypatch, restarted):
    instance = Runtime(tmp_path, tmp_path/'compose.json', [])
    base = {'services': {'backend': {}, 'worker': {}}}
    journal = {'mode': 'fleet', 'phase': 'starting-workers', 'targetBase': base,
               'staged': {}, 'candidate': 'backend-green',
               'candidateContainers': {'backend': 'backend-green', 'worker': 'worker-green'},
               'candidateStarts': {'backend': 'original', 'worker': 'original'}}
    save(instance.journal_file, journal)
    monkeypatch.setattr(instance, 'container', lambda service: service)
    monkeypatch.setattr(instance, 'dc', lambda *args: pytest.fail('resume must not recreate candidate'))
    monkeypatch.setattr(fleet.proxy, 'inspect', lambda _: {'State': {'StartedAt': 'new' if restarted else 'original'}})
    def not_ready(*args):
        raise RuntimeError('still not ready')
    monkeypatch.setattr(fleet, 'ready', not_ready)
    with pytest.raises(RuntimeError, match='Candidate restarted' if restarted else 'still not ready'):
        fleet._deploy(instance, base)
    assert read(instance.journal_file) == journal


def test_host_maintenance_uses_existing_backup_migration_and_promotion_owners(tmp_path, monkeypatch):
    layout = ops.runtime_layout(str(tmp_path), '/storage/onprem')
    directory = layout.state_dir / 'rolling'
    directory.mkdir(parents=True)
    manifest = {'runtimeContract': {'workerDrainVersion': 1},
                'images': {'deployment-proxy': 'nginx@sha256:test'}}
    save(layout.target_release_manifest_path, manifest)
    save(directory/'stack.json', {'configurationFingerprint': {}})
    events = []
    def event(label, result=None):
        return lambda *a, **k: events.append(label) or result
    adapter = SimpleNamespace(
        _compose_base_command=lambda _: ['docker', 'compose'], deployment_profile=lambda _: 'saas',
        snapshot_runtime=event('snapshot', tmp_path/'snapshot'), complete_full_backup=event('backup'),
        refresh_managed_sizing_profile=event('sizing'), render_compose=event('render'),
        render_logging_config=event('logging'), run_target_migrations=event('migrations'),
        docker_compose_up=event('up'), wait_for_health=event('health'),
        wait_for_agent_stream_gateway=event('gateway'), wait_for_doctor_ok=event('doctor'),
        _promote_release=event('promote', {'version': 'target'}))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='{"services":{}}'))
    def controller(runtime, **callbacks):
        events.append('drained')
        assert callbacks['prepare'](None) == {'services': {}}
        callbacks['start']()
        callbacks['verify']()
        callbacks['commit']()
        return {'status': 'ok'}
    monkeypatch.setattr(maintenance, 'deploy', controller)
    result = maintenance.upgrade(layout, SimpleNamespace(skip_image_pull=True, health_timeout=10),
        manifest, source='manifest', backup_mode='inline', backup_proof=None, ops=adapter)
    assert result == {'version': 'target', 'status': 'ok'}
    assert events == ['snapshot', 'drained', 'backup', 'sizing', 'render', 'logging',
                      'migrations', 'up', 'health', 'gateway', 'doctor', 'promote']
