import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from packetsafari_onprem import operations as ops, maintenance_update as maintenance
from packetsafari_onprem import fleet_update as fleet
from packetsafari_onprem.rolling_update import Runtime, activation_base, activation_complete, bootstrap, initialize_bootstrap_proxy, recover_incomplete_bootstrap, same_application_release, save, read


def test_activation_snapshot_keeps_dormant_logging_service(monkeypatch):
    def config(command, **kwargs):
        assert command[-5:] == ['--profile', '*', 'config', '--format', 'json']
        assert kwargs['check'] is True
        return SimpleNamespace(stdout=json.dumps({'services': {
            'backend': {'image': 'backend'},
            'audit-forwarder': {'image': 'forwarder', 'profiles': ['logging']},
        }}))

    monkeypatch.setattr('packetsafari_onprem.rolling_update.subprocess.run', config)
    services = activation_base(['docker', 'compose'])['services']
    assert services['audit-forwarder'] == {'image': 'forwarder', 'profiles': ['logging']}


def test_candidate_validation_includes_sizing_and_all_profiles_before_replace(tmp_path, monkeypatch):
    current = tmp_path / 'compose.yml'
    sizing = tmp_path / 'sizing.yml'
    current.write_text('serving')
    sizing.write_text('sizing')
    runtime = Runtime(tmp_path / 'rolling', current,
                      ['docker', 'compose', '-f', str(current), '-f', str(sizing)])

    def validate(command, **kwargs):
        assert command[-4:] == ['--profile', '*', 'config', '--quiet']
        assert str(sizing) in command
        candidate = Path(command[command.index('-f') + 1])
        assert candidate != current
        assert json.loads(candidate.read_text())['services']['audit-forwarder']['image'] == 'forwarder'
        assert current.read_text() == 'serving'
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr('packetsafari_onprem.rolling_update.subprocess.run', validate)
    runtime.validate_config({'services': {'audit-forwarder': {'image': 'forwarder'}}})
    assert current.read_text() == 'serving'


def test_failed_activation_can_reuse_only_unchanged_unconfigured_proxy(tmp_path):
    policy = {'mode': 'cloudfront-https', 'trustedCidrs': ['192.0.2.10/32'], 'viewerHttpsOnly': True}
    initialize_bootstrap_proxy(tmp_path, policy)
    original = (tmp_path / 'nginx.conf').read_bytes()
    initialize_bootstrap_proxy(tmp_path, policy)
    assert (tmp_path / 'nginx.conf').read_bytes() == original
    with pytest.raises(RuntimeError, match='refusing to overwrite'):
        initialize_bootstrap_proxy(tmp_path, {**policy, 'trustedCidrs': ['192.0.2.11/32']})


def test_bootstrap_sharkd_failure_restores_compose_and_removes_marker(tmp_path, monkeypatch):
    current = tmp_path / 'compose.yml'
    current.write_text('serving-compose')
    runtime = Runtime(tmp_path / 'rolling', current, ['docker', 'compose', '-f', str(current)])
    base = {'services': {'backend': {'image': 'backend', 'container_name': 'packetsafari-backend',
                                    'networks': {'app': {}}, 'ports': [{'target': 80, 'published': '8080'}]},
                         'sharkd': {'image': 'sharkd', 'networks': {'app': {}},
                                    'ports': [{'target': 4448, 'published': '4448'}]}},
            'networks': {'app': {}}}
    monkeypatch.setattr(runtime, 'validate_config', lambda _: None)
    monkeypatch.setattr(runtime, 'render', lambda _: None)
    calls = []
    failed = [False]

    def compose(*args):
        calls.append(args)
        if args[:4] == ('up', '-d', '--no-deps', 'sharkd') and not failed[0]:
            failed[0] = True
            raise RuntimeError('simulated Sharkd startup failure')
        return ''

    monkeypatch.setattr(runtime, 'dc', compose)
    monkeypatch.setattr('packetsafari_onprem.rolling_update.proxy.docker',
                        lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match='simulated Sharkd'):
        bootstrap(runtime, base, proxy_name='packetsafari-deployment-proxy', proxy_image='proxy')
    assert ('up', '-d', '--no-deps', 'sharkd') in calls
    assert current.read_text() == 'serving-compose'
    assert not runtime.stack_file.exists()


def test_retry_retains_proven_pre_transfer_marker(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    directory = state_dir / 'rolling'
    directory.mkdir(parents=True)
    runtime = Runtime(directory, tmp_path / 'compose.yml', ['docker', 'compose', '-f', str(tmp_path / 'compose.yml')])
    policy = {'mode': 'cloudfront-https', 'trustedCidrs': ['192.0.2.10/32'], 'viewerHttpsOnly': True}
    initialize_bootstrap_proxy(directory / 'proxy', policy)
    save(directory / 'activation.json', {'phase': 'app-installed'})
    save(runtime.stack_file, {'active': 'backend', 'proxyName': 'proxy'})
    monkeypatch.setattr('packetsafari_onprem.rolling_update.proxy.docker',
                        lambda *a, **kw: SimpleNamespace(returncode=1))
    monkeypatch.setattr('packetsafari_onprem.rolling_update.activation_base',
                        lambda _: {'services': {'backend': {'ports': [{'published': '8080'}]}}})
    assert recover_incomplete_bootstrap(runtime, SimpleNamespace(state_dir=state_dir), policy)
    assert not runtime.stack_file.exists()
    assert len(list(directory.glob('bootstrap-incomplete-*.json'))) == 1


def test_preliminary_stack_is_never_reported_as_completed_activation(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    directory = state_dir / 'rolling'
    (directory / 'proxy').mkdir(parents=True)
    save(directory / 'stack.json', {'active': 'backend', 'proxyName': 'proxy'})
    save(directory / 'proxy/state.json', {'generation': 'unconfigured'})
    layout = SimpleNamespace(state_dir=state_dir)
    assert not activation_complete(layout)
    save(directory / 'stack.json', {'active': 'backend', 'proxyName': 'proxy', 'fleetBases': {'backend': {}}})
    save(directory / 'proxy/state.json', {'generation': 'switched'})
    monkeypatch.setattr('packetsafari_onprem.rolling_update.proxy.inspect',
                        lambda _: {'State': {'Running': True}})
    assert activation_complete(layout)


def test_activation_accepts_signed_ops_only_revision_but_not_app_change():
    installed = {'version': '10.0.0-test', 'builtAt': '2026-09-25T00:00:00Z',
                 'images': {'backend': 'repo@sha256:' + 'a' * 64},
                 'tooling': {'version': '0.2.44'}}
    signed = {**installed, 'tooling': {'version': '0.2.47'},
              'opsOnlyPublication': {'imageDigestsReused': True}}
    assert same_application_release(installed, signed)
    assert not same_application_release(installed, {**signed, 'images': {'backend': 'repo@sha256:' + 'b' * 64}})


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
    args = SimpleNamespace(profile='saas')
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


@pytest.mark.parametrize('mode,profile,pending', [('fleet', 'saas', True), ('maintenance', 'saas', True), ('fleet', 'onprem', True), ('maintenance', 'onprem', False)])
def test_pending_host_update_uses_controller_checks_instead_of_installed_release_doctor(tmp_path, monkeypatch, mode, profile, pending):
    from packetsafari_onprem import rolling_update
    layout = ops.runtime_layout(str(tmp_path), str(tmp_path))
    ops.ensure_runtime_dirs(layout)
    (layout.state_dir/'rolling').mkdir(parents=True)
    if pending:
        save(layout.state_dir/'rolling/transaction.json', {'mode': mode, 'phase': 'draining'})
    save(layout.target_release_manifest_path, {'version': 'target', 'images': {}})
    layout.compose_file.write_text('fixture')
    for method in ('sync_bundle', 'report_backup_storage_preflight', 'maybe_self_update_tooling',
                   'validate_tooling_requirement', 'validate_upgrade_path', 'validate_manifest_profile',
                   'verify_saas_operator_authorization', 'verify_license_allows_release', 'validate_required_env'):
        monkeypatch.setattr(ops, method, lambda *a, **k: None)
    monkeypatch.setattr(ops, 'ensure_generated_upgrade_env', lambda *a, **k: [])
    monkeypatch.setattr(ops, 'supports_upgrade_host_actions', lambda *a, **k: True)
    monkeypatch.setattr(ops, 'prepare_connected_manifest', lambda *a, **k: layout.target_release_manifest_path)
    monkeypatch.setattr(ops, 'assert_upgrade_preflight_doctor', lambda *a: pytest.fail('old-release checks reject intentional drain') if pending else None)
    monkeypatch.setattr(rolling_update, 'enabled', lambda *a: True)
    visited = []
    owner = maintenance if mode == 'maintenance' else rolling_update
    monkeypatch.setattr(owner, 'upgrade', lambda *a, **k: visited.append(mode) or {'status': 'draining'})
    args = SimpleNamespace(runtime_root=str(tmp_path), container_runtime_root=str(tmp_path),
        profile=profile, backup_mode='skip', allow_unbacked_upgrade=True, manifest='saved',
        _host_requirements_report={}, _sizing_status_report={}, simulate_failure_phase='')
    assert ops.upgrade_release(args)['status'] == 'draining'
    assert visited == [mode]


@pytest.mark.parametrize('activated', [False, True])
def test_onprem_plan_defaults_to_maintenance_without_cloudfront(tmp_path, activated):
    layout = ops.runtime_layout(str(tmp_path), str(tmp_path))
    ops.ensure_runtime_dirs(layout)
    (layout.state_dir/'rolling').mkdir(parents=True)
    save(layout.deployment_state_path, {'deployment': {'profile': 'onprem'}})
    if activated:
        save(layout.state_dir/'rolling/stack.json', {'fleetBases': {'backend': {}}})
    plan = ops._deployment_plan(SimpleNamespace(), layout, {}, {'runtimeContract': {'protocolVersion': 1}}, 'inline')
    assert plan['mode'] == 'maintenance'
    assert plan['selectedBy'] == 'onprem-default'
    assert 'interrupt' in plan['message']
    assert 'No AWS or proxy activation is required' in plan['message']
    save(layout.state_dir/'rolling/transaction.json', {'mode': 'fleet', 'phase': 'draining'})
    assert ops._deployment_plan(SimpleNamespace(), layout, {}, {}, 'inline')['mode'] == 'fleet'
    assert not ops._default_onprem_maintenance('onprem', {'mode': 'fleet'})


def test_failed_drain_restores_old_service_before_any_changes(runtime, monkeypatch):
    instance, base, events = runtime
    def fail(*args):
        raise RuntimeError('worker exit 1')
    monkeypatch.setattr(maintenance.workload_drain, 'wait_worker', fail)
    with pytest.raises(RuntimeError, match='old service restored'):
        maintenance.deploy(instance, prepare=lambda _: pytest.fail('no migrations or rendering'),
                           start=lambda: pytest.fail('no new services'), target=base)
    assert ('start', 'worker') in events
    assert ('switch',) in events
    assert not instance.journal_file.exists()
    archived = list(instance.directory.glob('maintenance-aborted-*.json'))
    assert len(archived) == 1
    assert read(archived[0])['phase'] == 'aborted-before-changes'
    assert not any(event[0] == 'stop' for event in events)


def test_restore_failure_remains_resumable_without_preparing(runtime, monkeypatch):
    instance, base, events = runtime
    monkeypatch.setattr(maintenance.workload_drain, 'wait_worker', lambda *args: (_ for _ in ()).throw(RuntimeError('worker failed')))
    monkeypatch.setattr(maintenance, 'ready', lambda *args: (_ for _ in ()).throw(RuntimeError('not ready')))
    with pytest.raises(RuntimeError, match='not ready'):
        maintenance.deploy(instance, prepare=lambda _: pytest.fail('no changes'), start=lambda: None, target=base)
    assert read(instance.journal_file)['phase'] == 'restoring'
    monkeypatch.setattr(maintenance, 'ready', lambda *args: None)
    with pytest.raises(RuntimeError, match='old service restored'):
        maintenance.deploy(instance, prepare=lambda _: pytest.fail('no changes'), start=lambda: None, target=base)
    assert not instance.journal_file.exists()


@pytest.mark.parametrize('phase', ['preparing', 'starting', 'opening', 'committing'])
def test_cannot_restore_old_code_after_maintenance_changes(runtime, phase):
    instance, base, events = runtime
    with pytest.raises(RuntimeError, match='after maintenance changes'):
        maintenance.restore_before_changes(instance, {'phase': phase}, 1)
    assert not events
