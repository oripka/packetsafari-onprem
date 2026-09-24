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


def test_host_generation_resume_uses_saved_target_without_registry_pulls(tmp_path, monkeypatch):
    layout = ops.runtime_layout(str(tmp_path), '/storage/onprem')
    directory = layout.state_dir/'rolling'
    directory.mkdir(parents=True)
    base = {'services': {service: {'image': 'old'} for service in fleet.COHORT}}
    frozen = tmp_path/'runtime.env'
    frozen.write_text('fixture')
    from packetsafari_onprem.rolling_update import fingerprints
    save(directory/'stack.json', {'active': 'backend', 'fleetBases': {'backend': base},
                                 'configurationFingerprint': fingerprints([frozen])})
    target = {'services': {service: {'image': 'saved'} for service in fleet.COHORT}}
    save(directory/'transaction.json', {'mode': 'fleet', 'phase': 'draining', 'targetBase': target})
    manifest = {'version': 'target'}
    save(directory/'fleet-release.json', {'manifest': manifest, 'backupMode': 'skip', 'snapshot': str(tmp_path)})
    monkeypatch.setattr(fleet, 'release_images', lambda *a: dict.fromkeys(fleet.COHORT, 'signed'))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('resume must not pull or rediscover'))
    def resume(runtime, target_base, **kwargs):
        assert target_base == target
        return {'status': 'draining'}
    monkeypatch.setattr(fleet, 'deploy', resume)
    adapter = SimpleNamespace(_compose_base_command=lambda _: [])
    kwargs = dict(source='manifest', backup_mode='skip', backup_proof=None, ops=adapter)
    assert fleet.upgrade(layout, SimpleNamespace(), {}, manifest, **kwargs)['status'] == 'draining'
    with pytest.raises(RuntimeError, match='original backup policy'):
        fleet.upgrade(layout, SimpleNamespace(), {}, manifest, **{**kwargs, 'backup_mode': 'require-recent'})


def test_update_plan_explains_adoption_maintenance_and_pending_resume(tmp_path):
    layout = ops.runtime_layout(str(tmp_path), '/storage/onprem')
    (layout.state_dir/'rolling').mkdir(parents=True)
    args = SimpleNamespace()
    manifest = {'runtimeContract': {'protocolVersion': 1, 'workerDrainVersion': 1, 'schemaInputs': 'a'*64},
                'images': {service: 'repo/'+service+'@sha256:'+'a'*64 for service in fleet.COHORT}}
    assert ops._deployment_plan(args, layout, manifest, manifest, 'skip')['mode'] == 'activation-required'
    save(layout.state_dir/'rolling/stack.json', {'fleetBases': {'backend': {}}})
    assert ops._deployment_plan(args, layout, manifest, manifest, 'skip')['mode'] == 'fleet'
    assert ops._deployment_plan(args, layout, manifest, manifest, 'inline')['mode'] == 'maintenance-required'
    save(layout.state_dir/'rolling/transaction.json', {'mode': 'fleet', 'phase': 'committing'})
    plan = ops._deployment_plan(args, layout, manifest, manifest, 'skip')
    assert plan['pending'] and plan['phase'] == 'committing'
    assert 'resuming committing' in ops.format_update_plan({'deploymentPlan': plan}, {})


@pytest.mark.parametrize('mode', ['fleet', 'maintenance'])
def test_pending_host_update_uses_controller_checks_instead_of_installed_release_doctor(tmp_path, monkeypatch, mode):
    from packetsafari_onprem import rolling_update
    layout = ops.runtime_layout(str(tmp_path), str(tmp_path))
    ops.ensure_runtime_dirs(layout)
    (layout.state_dir/'rolling').mkdir(parents=True)
    save(layout.state_dir/'rolling/transaction.json', {'mode': mode, 'phase': 'draining'})
    save(layout.target_release_manifest_path, {'version': 'target', 'images': {}})
    layout.compose_file.write_text('fixture')
    for method in ('sync_bundle', 'report_backup_storage_preflight', 'maybe_self_update_tooling',
                   'validate_tooling_requirement', 'validate_upgrade_path', 'validate_manifest_profile',
                   'verify_saas_operator_authorization', 'validate_required_env'):
        monkeypatch.setattr(ops, method, lambda *a, **k: None)
    monkeypatch.setattr(ops, 'ensure_generated_upgrade_env', lambda *a, **k: [])
    monkeypatch.setattr(ops, 'supports_upgrade_host_actions', lambda *a, **k: True)
    monkeypatch.setattr(ops, 'prepare_connected_manifest', lambda *a, **k: layout.target_release_manifest_path)
    monkeypatch.setattr(ops, 'assert_upgrade_preflight_doctor', lambda *a: pytest.fail('old-release checks reject intentional drain'))
    monkeypatch.setattr(rolling_update, 'enabled', lambda *a: True)
    visited = []
    owner = maintenance if mode == 'maintenance' else rolling_update
    monkeypatch.setattr(owner, 'upgrade', lambda *a, **k: visited.append(mode) or {'status': 'draining'})
    args = SimpleNamespace(runtime_root=str(tmp_path), container_runtime_root=str(tmp_path),
        profile='saas', backup_mode='skip', allow_unbacked_upgrade=True, manifest='saved',
        _host_requirements_report={}, _sizing_status_report={}, simulate_failure_phase='')
    assert ops.upgrade_release(args)['status'] == 'draining'
    assert visited == [mode]
