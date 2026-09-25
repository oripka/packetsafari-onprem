from types import SimpleNamespace

import pytest

from packetsafari_onprem.schema_upgrade import plan
from packetsafari_onprem import fleet_update as fleet
from packetsafari_onprem.rolling_update import Runtime, save, read


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
