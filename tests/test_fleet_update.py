import copy
from pathlib import Path
import pytest
from types import SimpleNamespace
from packetsafari_onprem import fleet_update as fleet, workload_drain as drain


def base():
    services = {}
    for name in fleet.COHORT:
        services[name] = {'image': 'repo/' + name + '@sha256:' + 'a' * 64,
                          'container_name': name, 'environment': {}, 'volumes': [],
                          'networks': {'app': {'ipv4_address': '172.20.0.20'}}, 'ports': []}
    for name in ('worker', 'agent-cli-runner'):
        services[name]['volumes'] = [{'type': 'volume', 'source': 'runner', 'target': '/run/packetsafari-cli'}]
    services['agent-cli-runner'].pop('networks')
    services['agent-cli-runner']['network_mode'] = 'none'
    services['redis'] = {'image': 'redis@sha256:' + 'a' * 64}
    return {'services': services, 'volumes': {'runner': {'name': 'runner'}}, 'networks': {'app': {}}}


def test_generations_isolate_routes_sockets_and_network_policy_addresses():
    original = base()
    stack = {'active': 'backend', 'images': {'backend': 'blue', 'backend-green': 'green'},
             'proxyImage': 'proxy', 'proxyName': 'proxy', 'ports': [{'target': 8080, 'published': '8080'}],
             'sharkdPorts': [{'target': 4448, 'published': '4448'}],
             'fleetBases': {'backend': original, 'backend-green': copy.deepcopy(original)}}
    config = fleet.configuration(stack, Path('/runtime'))
    green = config['services']['worker-green']
    assert green['environment']['PACKETSAFARI_CAPTURE_SHARKD_HOST'] == 'sharkd-green'
    assert 'agent-stream-gateway-green' in green['environment']['NO_PROXY'].split(',')
    assert 'sharkd-green' in green['environment']['no_proxy'].split(',')
    assert green['volumes'][0]['source'] == 'runner-green'
    assert config['services']['worker']['volumes'][0]['source'] == 'runner'
    assert config['services']['agent-cli-runner-green']['volumes'][0]['source'] == 'runner-green'
    assert config['services']['agent-cli-runner-green']['network_mode'] == 'none'
    assert config['services']['backend-green']['environment']['PACKETSAFARI_AGENT_GATEWAY_HOST'] == 'agent-stream-gateway-green'
    assert config['services']['sharkd-green']['ports'] == []
    assert len(config['services']['deployment-proxy']['ports']) == 2
    assert green['profiles'] == ['rolling-inactive']
    assert original['services']['worker']['volumes'][0]['source'] == 'runner'


def test_shared_dependency_change_fails_closed():
    old, new = base(), base()
    new['services']['redis']['image'] = 'changed'
    with pytest.raises(ValueError, match='redis changed'):
        fleet.validate_shared(old, new)


@pytest.mark.parametrize('retiring_slot,service', [('backend', 'worker'), ('backend-green', 'worker-green')])
def test_drain_resume_resolves_compose_service_when_container_name_differs(tmp_path, monkeypatch, retiring_slot, service):
    from packetsafari_onprem.rolling_update import Runtime, save, read
    runtime = Runtime(tmp_path, tmp_path / 'compose.json', [])
    save(tmp_path / 'proxy/state.json', {'retiringWorkers': []})
    target = base()
    target['services']['worker']['container_name'] = 'packetsafari-worker'
    receipt = {'containerId': 'retiring-id', 'status': 'prepared'}
    save(runtime.journal_file, {
        'mode': 'fleet', 'phase': 'draining', 'targetBase': target,
        'candidate': 'backend-green' if retiring_slot == 'backend' else 'backend',
        'retiringSlot': retiring_slot, 'oldWorker': receipt, 'oldContainers': {'worker': 'retiring-id'},
        'staged': {'proxyName': 'proxy', 'fleetBases': {retiring_slot: target}},
    })
    calls = []
    def compose(*args):
        calls.append(args)
        assert args == ('ps', '-q', service), 'Compose requires the service key, not container_name'
        return 'retiring-id'
    monkeypatch.setattr(runtime, 'dc', compose)
    def rearm(old, container):
        assert old == receipt and container == 'retiring-id'
        return None
    monkeypatch.setattr(drain, 'rearm_worker', rearm)
    monkeypatch.setattr(drain, 'request_worker', lambda old, save: {**old, 'status': 'requested'})
    monkeypatch.setattr(drain, 'wait_worker', lambda old, timeout: {**old, 'status': 'draining'})
    monkeypatch.setattr(fleet.proxy, 'workers', lambda _: set())
    result = fleet._deploy(runtime, target, timeout=0)
    assert result['status'] == 'draining'
    assert calls == [('ps', '-q', service)]
    assert read(runtime.journal_file)['oldWorker']['status'] == 'draining'


def test_regular_release_accepts_all_application_image_changes():
    current = {'runtimeContract': {'protocolVersion': 1, 'workerDrainVersion': 1, 'schemaInputs': 'a'*64},
               'images': {name: 'repo/' + name + '@sha256:'+'a'*64 for name in fleet.COHORT}}
    new = copy.deepcopy(current)
    new['images'] = {name: value.replace('a'*64, 'b'*64) for name, value in new['images'].items()}
    assert set(fleet.release_images(current, new, 'skip')) == set(fleet.COHORT)
    new['runtimeContract']['schemaInputs'] = 'b'*64
    with pytest.raises(ValueError, match='maintenance required'):
        fleet.release_images(current, new, 'skip')


def worker(running=True, exit_code=0, started='one'):
    return {'Id': 'worker-id', 'Image': 'image', 'State': {'Running': running, 'ExitCode': exit_code, 'StartedAt': started},
            'HostConfig': {'RestartPolicy': {'Name': 'always'}}}


def test_timeout_retains_worker_and_never_escalates(monkeypatch):
    monkeypatch.setattr(drain.proxy, 'inspect', lambda _: worker())
    commands = []
    monkeypatch.setattr(drain.proxy, 'docker', lambda *a, **kw: commands.append(a))
    receipt = {'containerId': 'worker-id', 'imageId': 'image', 'startedAt': 'one', 'status': 'requested'}
    assert drain.wait_worker(receipt, timeout=0)['status'] == 'draining'
    assert commands == []


@pytest.mark.parametrize('state', [worker(False, 137), worker(False, 0, 'replacement')])
def test_failed_or_restarted_worker_is_not_a_drain(monkeypatch, state):
    monkeypatch.setattr(drain.proxy, 'inspect', lambda _: state)
    with pytest.raises(RuntimeError):
        drain.poll_worker({'containerId': 'worker-id', 'imageId': 'image', 'startedAt': 'one', 'status': 'requested'})


def test_signal_intent_is_durable_before_term_and_restart_is_disabled(monkeypatch):
    monkeypatch.setattr(drain.proxy, 'inspect', lambda _: worker())
    events = []
    monkeypatch.setattr(drain.proxy, 'docker', lambda *args: events.append(args))
    receipt = {'containerId': 'worker-id', 'imageId': 'image', 'startedAt': 'one', 'status': 'prepared'}
    drain.request_worker(receipt, lambda value: events.append(('saved', value['status'])))
    assert events == [('update', '--restart=no', 'worker-id'), ('saved', 'requested'),
                      ('kill', '--signal=TERM', 'worker-id')]


@pytest.mark.parametrize('http_status', [200, 302, 503])
def test_candidate_http_readiness_is_required_before_workers_start(monkeypatch, http_status):
    runtime = SimpleNamespace(compose_file='compose', stack_file='stack', container=lambda _: 'container')
    monkeypatch.setattr(fleet, 'read', lambda path: {'proxyName': 'proxy'} if path == 'stack' else
                        {'services': {'backend-green': {'image': 'image'}}})
    monkeypatch.setattr(fleet.proxy, 'inspect', lambda _: {'Id': 'image', 'Image': 'image', 'State': {'Running': True}})
    monkeypatch.setattr(fleet.proxy, 'address', lambda *a: '172.20.0.27:80')
    monkeypatch.setattr(fleet.proxy, 'docker', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stderr=f'HTTP/1.1 {http_status} result'))
    if http_status == 200:
        fleet.ready(runtime, ['backend-green'], timeout=0)
    else:
        with pytest.raises(RuntimeError, match='readiness timed out'):
            fleet.ready(runtime, ['backend-green'], timeout=0)


def test_worker_readiness_timeout_is_not_ready_not_fatal(monkeypatch):
    import subprocess
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired('docker exec', 15)
    monkeypatch.setattr(drain.proxy, 'docker', slow)
    assert drain.worker_ready('candidate') is False


def test_worker_readiness_uses_bounded_startup_probe_timeout(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='DRAIN_READY=true\n')
    monkeypatch.setattr(drain.proxy.subprocess, 'run', run)
    assert drain.worker_ready('candidate') is True
    assert calls[0][0][:3] == ['docker', 'exec', 'candidate']
    assert calls[0][1]['timeout'] == 60


def test_worker_restarted_before_drain_request_is_rearmed(monkeypatch):
    monkeypatch.setattr(drain.proxy, 'inspect', lambda _: worker(started='two'))
    monkeypatch.setattr(drain.proxy, 'docker', lambda *a, **kw: SimpleNamespace(stdout='running\n'))
    receipt = {'containerId': 'worker-id', 'imageId': 'image', 'startedAt': 'one', 'status': 'prepared'}
    rearmed = drain.rearm_worker(receipt, 'worker-green')
    assert rearmed['startedAt'] == 'two' and rearmed['status'] == 'prepared'


def test_unchanged_worker_is_not_rearmed(monkeypatch):
    monkeypatch.setattr(drain.proxy, 'inspect', lambda _: worker())
    receipt = {'containerId': 'worker-id', 'imageId': 'image', 'startedAt': 'one', 'status': 'prepared'}
    assert drain.rearm_worker(receipt, 'worker-green') is None


@pytest.mark.parametrize('status', ['requested', 'draining'])
def test_worker_restarted_after_drain_request_stays_unproven(monkeypatch, status):
    monkeypatch.setattr(drain.proxy, 'inspect', lambda _: worker(started='two'))
    receipt = {'containerId': 'worker-id', 'imageId': 'image', 'startedAt': 'one', 'status': status}
    assert drain.rearm_worker(receipt, 'worker-green') is None
    with pytest.raises(RuntimeError):
        drain.poll_worker(receipt)


def test_rearm_refuses_worker_without_warm_drain_supervisor(monkeypatch):
    monkeypatch.setattr(drain.proxy, 'inspect', lambda _: worker(started='two'))
    monkeypatch.setattr(drain.proxy, 'docker', lambda *a, **kw: SimpleNamespace(stdout='\n'))
    receipt = {'containerId': 'worker-id', 'imageId': 'image', 'startedAt': 'one', 'status': 'prepared'}
    with pytest.raises(RuntimeError, match='warm-drain supervisor'):
        drain.rearm_worker(receipt, 'worker-green')


def test_readiness_uses_configurable_default_timeout(monkeypatch):
    assert fleet.READY_TIMEOUT >= 300
    runtime = SimpleNamespace(compose_file='compose', stack_file='stack', container=lambda _: 'container')
    monkeypatch.setattr(fleet, 'read', lambda path: {'proxyName': 'proxy'} if path == 'stack' else
                        {'services': {'sharkd-green': {'image': 'image'}}})
    monkeypatch.setattr(fleet.proxy, 'inspect', lambda _: {'Id': 'image', 'Image': 'image',
                                                           'State': {'Running': True, 'Health': {'Status': 'starting'}}})
    monkeypatch.setattr(fleet, 'READY_TIMEOUT', 0)
    with pytest.raises(RuntimeError, match='readiness timed out'):
        fleet.ready(runtime, ['sharkd-green'])
