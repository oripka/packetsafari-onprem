import copy
import json
from pathlib import Path
import pytest
from packetsafari_onprem import rolling_update as rolling


def manifests():
    current = {'version': 'v1', 'images': {'backend': 'repo/api@sha256:'+'a'*64,
               'worker': 'repo/worker@sha256:'+'b'*64}}
    target = copy.deepcopy(current)
    target['version'] = 'v2'
    target['images']['agent-stream-gateway'] = current['images']['backend']
    target['images']['backend'] = 'repo/api@sha256:'+'c'*64
    target['rollingUpdate'] = {'compatibleFrom': ['v1']}
    return current, target


def test_exact_compatible_api_release():
    current, target = manifests()
    assert rolling.validate_release(current, target, 'skip') == target['images']['backend']


@pytest.mark.parametrize('change', ['worker', 'gateway', 'schema-declaration', 'tag', 'profile', 'inline'])
def test_unsafe_rolling_release_rejected(change):
    current, target = manifests()
    mode = 'skip'
    if change == 'worker':
        target['images']['worker'] = 'different'
    elif change == 'gateway':
        target['images'].pop('agent-stream-gateway')
    elif change == 'schema-declaration':
        target['rollingUpdate']['compatibleFrom'] = ['other-release']
    elif change == 'tag':
        target['images']['backend'] = 'repo/api:latest'
    elif change == 'profile':
        target['deploymentProfiles'] = {'saas': {}}
    elif change == 'inline':
        mode = 'inline'
    with pytest.raises(ValueError):
        rolling.validate_release(current, target, mode)


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


def test_compatibility_must_be_exact_list():
    current, target = manifests()
    target['rollingUpdate']['compatibleFrom'] = 'v10'
    with pytest.raises(ValueError):
        rolling.validate_release(current, target, 'skip')


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


def test_schema_change_never_switches(runtime, monkeypatch):
    instance, calls = runtime
    monkeypatch.setattr(instance, 'schema', lambda container: container)
    with pytest.raises(RuntimeError, match='Schema/model'):
        instance.deploy('new')
    assert rolling.read(instance.stack_file)['active'] == 'backend'
    assert rolling.read(instance.directory/'proxy/state.json')['containerId'] == 'old-container'
    assert ('stop', 'backend') not in calls
    assert not instance.journal_file.exists()


def test_failed_verification_restores_traffic_before_stopping_candidate(runtime):
    instance, calls = runtime
    def verify():
        assert rolling.read(instance.directory/'proxy/state.json')['containerId'] == 'new-container'
        raise ValueError('doctor failed')
    with pytest.raises(ValueError, match='doctor failed'):
        instance.deploy('new', verify=verify)
    assert rolling.read(instance.directory/'proxy/state.json')['containerId'] == 'old-container'
    assert ('stop', 'backend') not in calls
    assert calls[-1] == ('stop', 'backend-green')


def test_cleanup_failure_does_not_undo_committed_release(runtime, monkeypatch):
    instance, calls = runtime
    def command(*args):
        calls.append(args)
        if args == ('stop', 'backend'):
            raise RuntimeError('stop failed')
    monkeypatch.setattr(instance, 'dc', command)
    committed = []
    with pytest.raises(RuntimeError, match='stop failed'):
        instance.deploy('new', commit=lambda receipt: committed.append(receipt))
    assert committed
    assert rolling.read(instance.journal_file)['phase'] == 'committed'
    assert rolling.read(instance.stack_file)['active'] == 'backend-green'
    monkeypatch.setattr(instance, 'dc', lambda *args: calls.append(args))
    assert instance.recover()['status'] == 'committed'
    assert ('stop', 'backend-green') not in calls
    assert not instance.journal_file.exists()


def test_configuration_fingerprint_detects_env_changes(tmp_path):
    env = tmp_path/'runtime.env'
    env.write_text('setting=before')
    recorded = rolling.fingerprints([env])
    env.write_text('setting=after')
    assert rolling.fingerprints([env]) != recorded
