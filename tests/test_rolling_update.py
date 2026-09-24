import copy
import json
from pathlib import Path
import pytest
from packetsafari_onprem import rolling_update as rolling


def test_compose_retains_public_port_and_isolates_candidate():
    base = {'services': {'backend': {'image': 'old', 'container_name': 'backend',
            'networks': {'app': {'ipv4_address': '172.20.0.20'}},
            'ports': [{'target': 80, 'published': '8080'}],
            'environment': {'KEEP': 'value'}, 'volumes': ['/storage:/storage']},
            'frontend': {'environment': {}}}}
    original = copy.deepcopy(base)
    stack = {'active': 'backend', 'images': {'backend': 'old', 'backend-green': 'new'},
             'proxyImage': 'proxy@sha256:123', 'proxyName': 'proxy',
             'ports': [{'target': 8080, 'published': '8080'}]}
    config = rolling.compose_config(base, stack, Path('/private/rolling'))
    assert base == original
    assert config['services']['backend']['image'] == 'old'
    assert config['services']['backend-green']['image'] == 'new'
    assert config['services']['backend-green']['profiles'] == ['rolling-inactive']
    assert config['services']['backend-green']['ports'] == []
    assert config['services']['backend-green']['networks']['app']['ipv4_address'] == '172.20.0.27'
    assert config['services']['deployment-proxy']['ports'][0]['published'] == '8080'
    assert config['services']['frontend']['environment']['NUXT_INTERNAL_API_BASE'] == 'http://deployment-proxy:8080'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    instance = rolling.Runtime(tmp_path, tmp_path/'compose.json', [])
    (tmp_path/'proxy').mkdir()
    rolling.save(instance.stack_file, {'active': 'backend', 'proxyName': 'proxy',
                 'images': {'backend': 'old', 'backend-green': 'old'}})
    rolling.save(tmp_path/'proxy/state.json', {'containerId': 'old-container'})
    calls = []
    monkeypatch.setattr(instance, 'render', lambda stack: None)
    monkeypatch.setattr(instance, 'container', lambda slot: 'old-container' if slot == 'backend' else 'new-container')
    monkeypatch.setattr(instance, 'schema', lambda container: 'same')
    monkeypatch.setattr(instance, 'dc', lambda *args: calls.append(args))
    monkeypatch.setattr(rolling.proxy, 'workers', lambda name: set())
    def switch(directory, controller, candidate, **kwargs):
        rolling.save(directory/'state.json', {'containerId': candidate})
        return {'status': 'drained'}
    monkeypatch.setattr(rolling.proxy, 'switch', switch)
    return instance, calls


def test_legacy_mode_cannot_create_new_transactions(runtime):
    instance, calls = runtime
    with pytest.raises(RuntimeError, match='recovery-only'):
        instance.deploy('new')
    assert calls == []
    assert not instance.journal_file.exists()


def test_existing_legacy_transaction_can_restore_traffic(runtime):
    instance, calls = runtime
    old = rolling.read(instance.stack_file)
    rolling.save(instance.journal_file, {'phase': 'switched', 'oldStack': old,
                 'oldContainer': 'old-container', 'candidate': 'backend-green'})
    rolling.save(instance.directory/'proxy/state.json', {'containerId': 'new-container'})
    assert instance.recover()['status'] == 'rolled_back'
    assert calls == [('stop', 'backend-green')]
    assert not instance.journal_file.exists()


def test_legacy_committed_cleanup_remains_resumable(runtime, monkeypatch):
    instance, calls = runtime
    rolling.save(instance.journal_file, {'phase': 'committed', 'oldStack': {'active': 'backend'}})
    monkeypatch.setattr(instance, 'dc', lambda *args: (_ for _ in ()).throw(RuntimeError('stop failed')))
    with pytest.raises(RuntimeError, match='stop failed'):
        instance.recover()
    assert rolling.read(instance.journal_file)['phase'] == 'committed'
    monkeypatch.setattr(instance, 'dc', lambda *args: calls.append(args))
    assert instance.recover()['status'] == 'committed'
    assert calls == [('stop', 'backend')]


def test_configuration_fingerprint_detects_env_changes(tmp_path):
    env = tmp_path/'runtime.env'
    env.write_text('setting=before')
    recorded = rolling.fingerprints([env])
    env.write_text('setting=after')
    assert rolling.fingerprints([env]) != recorded
