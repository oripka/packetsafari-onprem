"""Identity-bound warm worker shutdown; a timeout never escalates to SIGKILL."""
from __future__ import annotations

import time
import json
import sys

from . import deployment_proxy as proxy


def worker_ready(container: str) -> bool:
    script = '''import json,os,socket
from packetsafari.celery_app import celery
names=['aichat','aichat-priority','index','index-priority','security']
if int(os.environ.get('PACKETSAFARI_RESERVED_ANALYSIS_SLOTS','0')) > 0:
 names += ['aichat-reserved','index-reserved']
expected={name+'@'+socket.gethostname() for name in names}
replies=celery.control.ping(destination=sorted(expected), timeout=1)
actual={name for reply in replies for name,value in reply.items() if value.get('ok')=='pong'}
print('DRAIN_READY='+json.dumps(expected <= actual))
'''
    result = proxy.docker('exec', container, 'python3', '-c', script, check=False)
    lines = [line for line in result.stdout.splitlines() if line.startswith('DRAIN_READY=')]
    return result.returncode == 0 and bool(lines) and json.loads(lines[-1].split('=', 1)[1]) is True


def begin_worker(container: str) -> dict:
    info = proxy.inspect(container)
    if not info['State']['Running']:
        raise RuntimeError('Cannot establish a drain contract for a stopped worker')
    protocol = proxy.docker('exec', info['Id'], 'cat', '/tmp/packetsafari-worker-drained').stdout.strip()
    if protocol != 'running':
        raise RuntimeError('Worker does not advertise the warm-drain supervisor; maintenance bootstrap required')
    return {'containerId': info['Id'], 'startedAt': info['State']['StartedAt'],
            'imageId': info['Image'], 'restartPolicy': info['HostConfig']['RestartPolicy'],
            'status': 'prepared'}


def poll_worker(receipt: dict) -> dict:
    info = proxy.inspect(receipt['containerId'])
    if (info['State']['StartedAt'] != receipt['startedAt'] or info['Image'] != receipt['imageId']):
        raise RuntimeError('Retiring worker identity changed; drain is not proven')
    if info['State'].get('OOMKilled') or info['State'].get('Dead'):
        raise RuntimeError('Retiring worker failed; cannot report a successful drain')
    if info['State']['Running']:
        return {**receipt, 'status': 'draining'}
    if info['State']['ExitCode'] != 0:
        raise RuntimeError('Retiring worker exited unsuccessfully; keep its dependencies for investigation')
    if receipt['status'] == 'prepared':
        raise RuntimeError('Worker exited before the drain request; completion is not proven')
    return {**receipt, 'status': 'drained'}


def request_worker(receipt: dict, save_receipt) -> dict:
    """Persist intent before signaling; the qualified supervisor ignores repeat TERM."""
    info = proxy.inspect(receipt['containerId'])
    if info['State']['StartedAt'] != receipt['startedAt'] or info['Image'] != receipt['imageId']:
        raise RuntimeError('Worker changed before drain request')
    if not info['State']['Running']:
        return poll_worker(receipt)
    proxy.docker('update', '--restart=no', receipt['containerId'])
    requested = {**receipt, 'status': 'requested'}
    save_receipt(requested)
    proxy.docker('kill', '--signal=TERM', receipt['containerId'])
    return requested


def wait_worker(receipt: dict, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    next_progress = time.monotonic()
    while True:
        result = poll_worker(receipt)
        if result['status'] == 'drained' or time.monotonic() >= deadline:
            return result
        if time.monotonic() >= next_progress:
            print('Deployment drain: old worker is finishing work; its dependencies remain running', file=sys.stderr, flush=True)
            next_progress = time.monotonic() + 5
        time.sleep(0.25)
