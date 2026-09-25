#!/usr/bin/env python3
"""Isolated Docker generation test with real Celery/Redis and synthetic jobs."""
import copy
from contextlib import closing
import http.client
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packetsafari_onprem import fleet_update as fleet, deployment_proxy as proxy, workload_drain
from packetsafari_onprem.rolling_update import Runtime, save, read


def main():
    root = Path(os.environ.get('PACKETSAFARI_DATA_ROOT', '/Users/otr/packetsafari-data')) / 'runs/deployment-proxy' / ('fleet-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    project = 'ps-fleet-' + root.name[-8:]
    networks = proxy.docker('network', 'ls', '-q').stdout.split()
    used = [ipaddress.ip_network(item['Subnet']) for network in json.loads(proxy.docker('network', 'inspect', *networks).stdout)
            for item in (network.get('IPAM', {}).get('Config') or []) if item.get('Subnet') and ':' not in item['Subnet']]
    subnet = next(f'172.28.{n}.' for n in range(1, 255)
                  if not any(ipaddress.ip_network(f'172.28.{n}.0/24').overlaps(old) for old in used))
    fixture = root / 'fixture'
    (fixture / 'packetsafari').mkdir(parents=True)
    (fixture / 'packetsafari/__init__.py').write_text('')
    (fixture / 'packetsafari/celery_app.py').write_text('''import os,time
from celery import Celery
celery=Celery('fixture',broker='redis://redis:6379/0',backend='redis://redis:6379/1')
celery.conf.task_track_started=True
@celery.task(name='fixture.long')
def long(seconds):
 time.sleep(seconds)
 return {'generation':os.environ['SLOT'],'pid':os.getpid()}
''')
    backend = Path(__file__).resolve().parents[1] / 'tests/fixtures/deployment_backend.py'
    supervisor = Path(__file__).resolve().parents[2] / 'packetsafari/backend/scripts/worker_supervisor.sh'
    worker_image = proxy.inspect('worker-tests')['Image']
    redis_image = proxy.inspect('redis:8.2.9-bookworm')['Id']
    # Config-only image variants isolate deployment behavior, not application fidelity.
    images = {}
    for slot in ('blue', 'green'):
        created = proxy.docker('create', worker_image, 'true').stdout.strip()
        images[slot] = proxy.docker('commit', '--change', f'LABEL fixture.generation={slot}', created).stdout.strip()
    online = os.environ.get('PACKETSAFARI_TEST_ONLINE_SCHEMA_IMAGE')
    schema = None
    if online:
        from online_schema_fixture import prepare_images
        images, schema = prepare_images(root, online)
    services = {}
    addresses = {'backend': 20, 'worker': 21, 'agent-stream-gateway': 22, 'sharkd': 23}
    for logical in fleet.COHORT:
        port = 4448 if logical == 'sharkd' else 80
        services[logical] = {'image': images['blue'], 'container_name': project+'-'+logical,
            'entrypoint': [], 'working_dir': '/fixture', 'command': ['python3', '/backend.py'],
            'environment': {'SLOT': 'blue', 'PORT': str(port), 'PYTHONPATH': '/fixture'},
            'volumes': [{'type': 'bind', 'source': str(fixture), 'target': '/fixture', 'read_only': True},
                        {'type': 'bind', 'source': str(backend), 'target': '/backend.py', 'read_only': True},
                        {'type': 'bind', 'source': str(supervisor), 'target': '/app/scripts/worker_supervisor.sh', 'read_only': True}],
            'ports': [], 'restart': 'always'}
        if logical in addresses:
            services[logical]['networks'] = {'app': {'ipv4_address': '172.20.0.'+str(addresses[logical])}}
        else:
            services[logical]['network_mode'] = 'none'
            services[logical]['command'] = ['sleep', '3600']
    if online:
        for service in services.values():
            service['volumes'] = service['volumes'][:2]
            service['environment'].update(PACKETSAFARI_RUNTIME_POSTGRES_URL='postgresql+psycopg2://postgres@postgres:5432/postgres',
                                          PACKETSAFARI_AUTH_JWT_SECRET_KEY='isolated-fixture-only')
    command = '''set -eu
source /app/scripts/worker_supervisor.sh
for queue in aichat aichat_priority index index_priority security; do
 node="${queue//_/-}"
 celery -A packetsafari.celery_app worker --loglevel=warning --pool=solo --concurrency=1 --without-gossip --without-mingle --queues="$queue" --hostname="$node@%h" & PIDS+=($!)
done
worker_wait
'''
    services['worker']['command'] = ['bash', '-c', command.replace('$', '$$')]
    services['redis'] = {'image': redis_image, 'container_name': project+'-redis',
                         'networks': {'app': {'ipv4_address': '172.20.0.11'}},
                         'command': ['redis-server', '--save', '', '--appendonly', 'no']}
    if online:
        services['postgres'] = {'image': proxy.inspect('postgres')['Image'], 'container_name': project+'-postgres',
            'networks': {'app': {'ipv4_address': '172.20.0.10'}},
            'environment': {'POSTGRES_HOST_AUTH_METHOD': 'trust'}}
    for logical in ('backend', 'agent-stream-gateway', 'sharkd'):
        port = services[logical]['environment']['PORT']
        services[logical]['healthcheck'] = {'test': ['CMD', 'python3', '-c', f'import urllib.request; urllib.request.urlopen("http://127.0.0.1:{port}/api/v2/health", timeout=1)'], 'interval': '1s', 'timeout': '2s', 'retries': 3}
    base = {'services': services, 'networks': {'app': {'ipam': {'config': [{'subnet': '172.20.0.0/24'}]}}}, 'volumes': {}}
    stack = {'active': 'backend', 'images': {'backend': images['blue'], 'backend-green': images['blue']},
             'proxyImage': proxy.PROXY_IMAGE, 'proxyName': project+'-proxy',
             'ports': [{'target': 8080, 'host_ip': '127.0.0.1'}],
             'sharkdPorts': [{'target': 4448, 'host_ip': '127.0.0.1'}],
             'fleetBases': {'backend': base, 'backend-green': copy.deepcopy(base)}}
    # Avoid the real dev subnet while exercising the actual production renderer.
    original_configuration = fleet.configuration
    def configuration(stack, directory):
        return json.loads(json.dumps(original_configuration(stack, directory)).replace('172.20.0.', subnet))
    fleet.configuration = configuration
    save(root/'base.json', base)
    save(root/'stack.json', stack)
    proxy.initialize(root/'proxy')
    runtime = Runtime(root, root/'compose.json', ['docker', 'compose', '-p', project, '-f', str(root/'compose.json')])
    runtime.render(stack)
    events = []
    samples = []
    stop = threading.Event()
    poller = None
    try:
        runtime.dc('up', '-d', '--pull', 'never')
        fleet.ready(runtime, list(fleet.COHORT), timeout=120)
        if online:
            for _ in range(100):
                if proxy.docker('exec', runtime.container('postgres'), 'pg_isready', '-U', 'postgres', check=False).returncode == 0:
                    break
                time.sleep(.1)
            proxy.docker('exec', runtime.container('backend'), '/usr/bin/env',
                'PACKETSAFARI_SKIP_SERVICE_INIT=true', 'python3', '/app/scripts/sql_storage_upgrade.py', 'upgrade', 'head')
        proxy.switch(root/'proxy', stack['proxyName'], runtime.container('backend'), sharkd=runtime.container('sharkd'))
        port = int(runtime.dc('port', 'deployment-proxy', '8080').rsplit(':', 1)[1])
        def poll():
            while not stop.is_set():
                started, error = time.monotonic(), None
                try:
                    with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=2)) as connection:
                        connection.request('GET', '/')
                        response = connection.getresponse()
                        response.read()
                        assert response.status == 200, response.status
                except Exception as exc:
                    error = str(exc)
                samples.append({'seconds': time.monotonic()-started, 'error': error})
                stop.wait(.025)
        poller = threading.Thread(target=poll)
        poller.start()
        old = {name: runtime.container(name) for name in fleet.COHORT}
        def task(container, seconds, direct=False):
            script = "import socket\nfrom packetsafari.celery_app import celery\nqueue='index'\n"
            if direct:
                script += "queue='fixture-'+socket.gethostname()\nassert celery.control.add_consumer(queue,destination=['index@'+socket.gethostname()],reply=True,timeout=3)\n"
            script += f"print('TASK='+celery.send_task('fixture.long',args=[{seconds}],queue=queue).id)"
            output = proxy.docker('exec', container, 'python3', '-c', script).stdout
            return next(line[5:] for line in output.splitlines() if line.startswith('TASK='))
        task_id = task(old['worker'], 40)
        def result(task_id):
            # Result storage survives maintenance while both API slots are stopped.
            output = proxy.docker('exec', runtime.container('redis'), 'redis-cli', '-n', '1', '--raw',
                                  'GET', 'celery-task-meta-' + task_id).stdout.strip()
            stored = json.loads(output) if output else {}
            return {'state': stored.get('status', 'PENDING'), 'result': stored.get('result')}
        for _ in range(100):
            if result(task_id)['state'] == 'STARTED':
                break
            time.sleep(.1)
        else:
            raise AssertionError('Old worker did not claim long task')
        target = copy.deepcopy(base)
        for logical in fleet.COHORT:
            target['services'][logical]['image'] = images['green']
            target['services'][logical]['environment']['SLOT'] = 'green'
        commits = []
        outcome = fleet.deploy(runtime, target, commit=lambda r: commits.append(r), timeout=5, schema=schema)
        assert outcome['status'] == 'draining', outcome
        assert commits == []
        assert all(proxy.inspect(container)['State']['Running'] for container in old.values())
        port = int(runtime.dc('port', 'deployment-proxy', '8080').rsplit(':', 1)[1])
        with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=3)) as connection:
            connection.request('GET', '/')
            response = connection.getresponse()
            assert response.status == 200 and json.loads(response.read())['slot'] == 'green'
        new_task = task(runtime.container('worker-green'), 0)
        for _ in range(100):
            if result(new_task)['state'] == 'SUCCESS':
                break
            time.sleep(.1)
        assert result(new_task)['result']['generation'] == 'green'
        events.append({'check': 'new API and new task use green while blue job and dependencies remain alive'})
        outcome = fleet.deploy(runtime, target, commit=lambda r: commits.append(r), timeout=60)
        assert outcome['status'] == 'ok', outcome
        assert result(task_id)['state'] == 'SUCCESS'
        assert result(task_id)['result']['generation'] == 'blue'
        assert len(commits) == 1
        assert all(not proxy.inspect(container)['State']['Running'] for container in old.values())
        assert all(proxy.inspect(runtime.container(fleet.name(name, 'backend-green')))['Image'] == images['green'] for name in fleet.COHORT)
        events.append({'check': 'old task completed on blue; all old application containers stopped after drain; all green images verified'})
        if online:
            revision = proxy.docker('exec', runtime.container('postgres'), 'psql', '-U', 'postgres', '-Atc',
                'SELECT version_num FROM alembic_version').stdout.strip()
            assert revision == schema['targetRevision'], revision
            assert proxy.docker('exec', runtime.container('postgres'), 'psql', '-U', 'postgres', '-Atc',
                "SELECT to_regclass('online_schema_fixture') IS NOT NULL").stdout.strip() == 't'
            assert samples and not any(s['error'] for s in samples)
            events.append({'check': 'real online schema migration, API switch, worker drain, retirement with no sampled HTTP failures'})
            from online_schema_fixture import test_followup_updates
            test_followup_updates(root, runtime, target, samples, events)
            save(root/'result.json', {'events': events, 'requests': len(samples), 'errors': 0, 'schemaPlan': schema})
            print(json.dumps({'events': events, 'evidence': str(root)}, indent=2))
            return
        # Verification fails after the replacement worker has accepted work.
        # Roll traffic back, but preserve that worker and its dependencies too.
        rollback_tasks = []
        def fail_verification():
            rollback_tasks.append(task(runtime.container('worker'), 25, direct=True))
            for _ in range(100):
                if result(rollback_tasks[-1])['state'] == 'STARTED':
                    break
                time.sleep(.1)
            raise RuntimeError('injected post-switch verification failure')
        outcome = fleet.deploy(runtime, base, verify=fail_verification, timeout=5)
        assert outcome['status'] == 'draining', outcome
        assert read(root/'stack.json')['active'] == 'backend-green'
        assert proxy.inspect(runtime.container('worker'))['State']['Running']
        outcome = fleet.deploy(runtime, base, verify=fail_verification, timeout=60)
        assert outcome['status'] == 'rolled_back', outcome
        assert len(rollback_tasks) == 1
        assert result(rollback_tasks[0])['state'] == 'SUCCESS'
        assert result(rollback_tasks[0])['result']['generation'] == 'blue'
        events.append({'check': 'failed post-switch verification rolled traffic back while candidate job finished before its containers stopped'})
        bad = copy.deepcopy(base)
        bad['services']['backend']['command'] = ['bash', '-lc', 'touch /tmp/unhealthy; exec python3 /backend.py']
        try:
            fleet.deploy(runtime, bad, timeout=4)
            raise AssertionError('Unhealthy candidate accepted')
        except RuntimeError as exc:
            assert 'readiness' in str(exc).lower(), str(exc)
        assert not runtime.journal_file.exists()
        assert read(root/'stack.json')['active'] == 'backend-green'
        assert not runtime.dc('ps', '-q', 'worker')
        events.append({'check': 'unhealthy candidate rejected and cleaned before its workers could start; serving generation unchanged'})
        # Workers can already own tasks when their final readiness check fails.
        # The same abort path must preserve those tasks without requiring readiness.
        original_ready = fleet.ready
        abort_tasks = []
        def fail_worker_readiness(runtime_arg, services, timeout=120):
            original_ready(runtime_arg, services, timeout)
            if 'worker' in services:
                abort_tasks.append(task(runtime.container('worker'), 20, direct=True))
                for _ in range(100):
                    if result(abort_tasks[-1])['state'] == 'STARTED':
                        break
                    time.sleep(.1)
                raise RuntimeError('injected worker readiness failure')
        fleet.ready = fail_worker_readiness
        try:
            fleet.deploy(runtime, base, timeout=60)
            raise AssertionError('Failed worker readiness accepted')
        except RuntimeError as exc:
            assert 'worker readiness failure' in str(exc), str(exc)
        finally:
            fleet.ready = original_ready
        assert read(runtime.journal_file)['phase'] == 'starting-workers'
        assert fleet.abort(runtime, timeout=1)['status'] == 'draining'
        assert proxy.inspect(runtime.container('agent-cli-runner'))['State']['Running']
        assert fleet.abort(runtime, timeout=60)['status'] == 'rolled_back'
        assert result(abort_tasks[0])['state'] == 'SUCCESS'
        assert read(runtime.stack_file)['active'] == 'backend-green'
        events.append({'check': 'abort after worker readiness failure drained candidate-owned job and retained its dependencies'})
        assert not any(sample['error'] for sample in samples)
        stop.set()
        poller.join()
        # Explicit maintenance is allowed to return 503, but may not kill work.
        from packetsafari_onprem import maintenance_update as maintenance
        maintenance_task = task(runtime.container('worker-green'), 20, direct=True)
        for _ in range(100):
            if result(maintenance_task)['state'] == 'STARTED':
                break
            time.sleep(.1)
        prepared, starts = [], []
        def prepare_maintenance(target):
            assert result(maintenance_task)['state'] == 'SUCCESS'
            prepared.append(True)
            return target
        def start_maintenance():
            starts.append(True)
            if len(starts) == 1:
                raise RuntimeError('injected interruption before maintenance startup')
            runtime.dc('up', '-d', '--pull', 'never')
        kwargs = dict(target=base, prepare=prepare_maintenance, start=start_maintenance)
        assert maintenance.deploy(runtime, **kwargs, timeout=1)['status'] == 'draining'
        with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=2)) as connection:
            connection.request('GET', '/')
            response = connection.getresponse()
            assert response.status == 503
            response.read()
        assert proxy.inspect(runtime.container('sharkd-green'))['State']['Running']
        try:
            maintenance.deploy(runtime, **kwargs, timeout=60)
            raise AssertionError('Injected startup failure did not propagate')
        except RuntimeError as exc:
            assert 'interruption before maintenance startup' in str(exc), str(exc)
        assert read(runtime.journal_file)['phase'] == 'starting'
        assert maintenance.deploy(runtime, **kwargs, timeout=60)['status'] == 'ok'
        assert prepared == [True], 'Interrupted startup must not redo backup/target preparation'
        assert not runtime.journal_file.exists()
        assert read(runtime.stack_file)['active'] == 'backend'
        with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=2)) as connection:
            connection.request('GET', '/')
            response = connection.getresponse()
            assert response.status == 200 and json.loads(response.read())['slot'] == 'blue'
        events.append({'check': 'maintenance returned 503, drained the active job, resumed interrupted startup without re-preparing, and restored blue ingress'})
        save(root/'result.json', {'events': events, 'task': task_id, 'newTask': new_task, 'evidence': str(root),
                                 'requests': len(samples), 'errors': 0, 'maxRequestSeconds': max(s['seconds'] for s in samples)})
        print(json.dumps({'events': events, 'evidence': str(root)}, indent=2))
    finally:
        stop.set()
        if poller:
            poller.join()
        save(root/'availability.json', samples)
        # This fixture has no customer tasks or storage. Retain stopped containers/logs.
        runtime.dc('stop', '--timeout', '1', *fleet.COHORT,
                   *[fleet.name(s, 'backend-green') for s in fleet.COHORT], 'deployment-proxy', 'redis', *(['postgres'] if online else []))


if __name__ == '__main__':
    main()
