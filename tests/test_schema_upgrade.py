from types import SimpleNamespace

import pytest

from packetsafari_onprem.schema_upgrade import plan
from packetsafari_onprem import fleet_update as fleet
from packetsafari_onprem.rolling_update import Runtime, save, read
from packetsafari_onprem import operations as ops


def contract(rows=None, identity='a'):
    return {'protocolVersion': 1, 'workerDrainVersion': 1, 'schemaInputs': identity * 64,
            'schemaMigrations': {'format': 1, 'environment': 'c' * 64,
                                 'files': rows or [row('old', None)]}}


def row(revision, parent, compatible=False):
    return {'revision': revision, 'parent': parent, 'sha256': 'd' * 64,
            'path': 'alembic/versions/' + revision + '.py', 'rollingCompatible': compatible}


def test_reviewed_append_and_model_only_changes_are_distinct():
    old = contract()
    assert plan(old, contract(identity='b'))['migrations'] == []
    new = contract([row('old', None), row('new', 'old', True)], 'b')
    assert plan(old, new)['migrations'] == ['new']
    legacy = {k: v for k, v in old.items() if k != 'schemaMigrations'}
    assert plan(legacy, old)['migrations'] == []
    with pytest.raises(ValueError, match='legacy'):
        plan(legacy, new)


def test_established_merge_history_can_be_followed_by_a_linear_addition():
    rows = [row('root', None), row('left', 'root'), row('right', 'root'), row('merge', ['left', 'right'])]
    assert plan(contract(rows), contract(rows + [row('next', 'merge', True)], 'b'))['migrations'] == ['next']


@pytest.mark.parametrize('change', ['unreviewed', 'rewrite', 'remove', 'environment', 'branch', 'cycle', 'protocol'])
def test_incompatible_or_ambiguous_changes_require_maintenance(change):
    old = contract()
    new = contract([row('old', None), row('new', 'old', True)], 'b')
    files = new['schemaMigrations']['files']
    if change == 'unreviewed': files[-1]['rollingCompatible'] = False
    if change == 'rewrite': files[0]['sha256'] = 'f' * 64
    if change == 'remove': files.pop(0)
    if change == 'environment': new['schemaMigrations']['environment'] = 'e' * 64
    if change == 'branch': files.append(row('branch', 'old', True))
    if change == 'cycle': files[-1]['parent'] = 'new'
    if change == 'protocol': new['protocolVersion'] = 2
    with pytest.raises(ValueError):
        plan(old, new)


def test_migration_failure_preserves_serving_generation_and_saved_resume(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path, tmp_path/'compose.json', ['docker', 'compose'])
    target = {'services': {'backend': {'image': 'signed-target'}}}
    schema = plan(contract(), contract([row('old', None), row('new', 'old', True)], 'b'))
    journal = {'mode': 'fleet', 'phase': 'migrating', 'id': 'test', 'targetBase': target,
               'candidate': 'backend-green', 'staged': {}, 'schemaPlan': schema,
               'oldContainers': {'backend': 'old-backend'}}
    save(runtime.journal_file, journal)
    monkeypatch.setattr(runtime, 'schema', lambda _: 'a' * 64)
    monkeypatch.setattr(runtime, 'validate_config', lambda _: None)
    monkeypatch.setattr(fleet, 'configuration', lambda *a: target)
    commands = []
    monkeypatch.setattr(runtime, 'dc', lambda *a: commands.append(a))
    def interrupted(*a):
        raise KeyboardInterrupt('interrupted migration')
    monkeypatch.setattr(fleet, 'run_online_migration', interrupted)
    with pytest.raises(KeyboardInterrupt):
        fleet.deploy(runtime, target)
    assert read(runtime.journal_file)['phase'] == 'migrating'
    assert commands == []  # No candidate startup, traffic switch, or old service stop.
    events = []
    monkeypatch.setattr(fleet, 'run_online_migration', lambda *a: events.append('migration verified'))
    def next_phase(*a):
        assert events == ['migration verified']
        raise RuntimeError('stop test after entering candidate readiness')
    monkeypatch.setattr(fleet, 'ready', next_phase)
    with pytest.raises(RuntimeError, match='stop test'):
        fleet._deploy(runtime, target)
    assert read(runtime.journal_file)['phase'] == 'preparing'
    with pytest.raises(RuntimeError, match='stop test'):
        fleet._deploy(runtime, target)
    assert events == ['migration verified']  # Prepared-phase resume cannot repeat migration.


def test_live_input_drift_blocks_migration_before_any_sql(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path, tmp_path/'compose.json', [])
    target = {'services': {}}
    save(runtime.journal_file, {'mode': 'fleet', 'phase': 'migrating', 'targetBase': target,
        'staged': {}, 'candidate': 'backend-green', 'oldContainers': {'backend': 'old'},
        'schemaPlan': {'inputs': ['a'*64, 'b'*64], 'migrations': ['new']}})
    monkeypatch.setattr(runtime, 'schema', lambda _: 'changed')
    monkeypatch.setattr(fleet, 'run_online_migration', lambda *a: pytest.fail('must not migrate'))
    with pytest.raises(RuntimeError, match='Serving schema inputs'):
        fleet.deploy(runtime, target)


@pytest.mark.parametrize('mount', ['/app', '/app/alembic/env.py', '/app/scripts', '/app/packetsafari'])
def test_mutable_schema_code_cannot_override_the_signed_migration_image(mount):
    journal = {'schemaPlan': {}, 'targetBase': {'services': {'backend': {'volumes': [{'target': mount}]}}}}
    with pytest.raises(ValueError, match='immutable image code'):
        fleet.run_online_migration(None, journal)


def test_existing_migration_container_is_not_replaced_or_killed(monkeypatch):
    journal = {'id': 'test', 'schemaPlan': {}, 'targetBase': {'services': {'backend': {}}}}
    calls = []
    monkeypatch.setattr(fleet.proxy, 'docker', lambda *a, **kw: calls.append(a) or SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match='still exists'):
        fleet.run_online_migration(None, journal)
    assert calls == [('inspect', 'packetsafari-online-schema-test')]


@pytest.mark.parametrize('reviewed', [True, False])
def test_normal_cli_plan_explains_online_or_required_downtime(tmp_path, capsys, reviewed):
    layout = ops.runtime_layout(str(tmp_path), '/storage/onprem')
    (layout.state_dir/'rolling').mkdir(parents=True)
    save(layout.state_dir/'rolling/stack.json', {'fleetBases': {'backend': {}}})
    images = {service: 'repo/'+service+'@sha256:'+'a'*64 for service in fleet.COHORT}
    old = {'runtimeContract': contract(), 'images': images}
    new = {'runtimeContract': contract([row('old', None), row('new', 'old', reviewed)], 'b'), 'images': images}
    result = ops._deployment_plan(SimpleNamespace(profile='saas'), layout, old, new, 'skip')
    ops.report_update_strategy(result)
    output = capsys.readouterr()
    assert output.out == ''  # Keep machine-readable stdout clean.
    if reviewed:
        assert result['mode'] == 'fleet'
        assert result['interruption'] == 'none-planned'
        assert result['onlineMigrations'] == ['new']
        assert 'No --maintenance flag is needed' in output.err
        assert 'Online schema revisions: new' in output.err
    else:
        assert result['mode'] == 'maintenance-required'
        assert result['interruption'] == 'required'
        assert 'WARNING: maintenance required' in output.err
        assert 'Downtime is required' in output.err
        assert 'Repeat the same command with --maintenance' in output.err


@pytest.mark.parametrize('reviewed', [True, False])
def test_host_dispatch_never_silently_selects_maintenance(tmp_path, monkeypatch, capsys, reviewed):
    from packetsafari_onprem import rolling_update, maintenance_update
    layout = ops.runtime_layout(str(tmp_path), str(tmp_path))
    ops.ensure_runtime_dirs(layout)
    (layout.state_dir/'rolling').mkdir(parents=True)
    save(layout.state_dir/'rolling/stack.json', {'fleetBases': {'backend': {}}})
    images = {s: 'repo/'+s+'@sha256:'+'a'*64 for s in fleet.COHORT}
    save(layout.release_manifest_path, {'runtimeContract': contract(), 'images': images})
    save(layout.target_release_manifest_path, {'runtimeContract': contract(
        [row('old', None), row('new', 'old', reviewed)], 'b'), 'images': images})
    layout.compose_file.write_text('fixture')
    for method in ('sync_bundle', 'report_backup_storage_preflight', 'maybe_self_update_tooling',
                   'validate_tooling_requirement', 'validate_upgrade_path', 'validate_manifest_profile',
                   'verify_saas_operator_authorization', 'validate_required_env', 'assert_upgrade_preflight_doctor'):
        monkeypatch.setattr(ops, method, lambda *a, **k: None)
    monkeypatch.setattr(ops, 'ensure_generated_upgrade_env', lambda *a, **k: [])
    monkeypatch.setattr(ops, 'supports_upgrade_host_actions', lambda *a, **k: True)
    monkeypatch.setattr(ops, 'prepare_connected_manifest', lambda *a, **k: layout.target_release_manifest_path)
    monkeypatch.setattr(rolling_update, 'enabled', lambda *a: True)
    called = []
    monkeypatch.setattr(rolling_update, 'upgrade', lambda *a, **k: called.append('fleet') or {'status': 'ok'})
    monkeypatch.setattr(maintenance_update, 'upgrade', lambda *a, **k: pytest.fail('no maintenance authorization'))
    args = SimpleNamespace(runtime_root=str(tmp_path), container_runtime_root=str(tmp_path),
        profile='saas', backup_mode='skip', allow_unbacked_upgrade=True, manifest='saved',
        _host_requirements_report={}, _sizing_status_report={}, simulate_failure_phase='')
    if reviewed:
        assert ops.upgrade_release(args)['status'] == 'ok'
        assert called == ['fleet']
    else:
        with pytest.raises(RuntimeError, match='Upgrade failed during preflight:.*Downtime is required'):
            ops.upgrade_release(args)
        assert called == []
        assert 'WARNING: maintenance required' in capsys.readouterr().err
