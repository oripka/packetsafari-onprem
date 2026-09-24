#!/usr/bin/env python3
"""Explicit local Docker integration test. Own project only; no production access."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packetsafari_onprem.deployment_proxy import PROXY_IMAGE, initialize, switch, docker


def main():
    run = Path(os.environ.get('PACKETSAFARI_DATA_ROOT', '/Users/otr/packetsafari-data')) / 'runs' / 'deployment-proxy' / uuid.uuid4().hex
    run.mkdir(parents=True)
    state = run / 'proxy'
    initialize(state)
    project = 'ps-proxy-test-' + run.name[:8]
    fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/deployment_backend.py'
    services = {'proxy': {'image': PROXY_IMAGE, 'command': ['nginx', '-g', 'daemon off;', '-c', '/etc/packetsafari-proxy/nginx.conf'],
                         'ports': ['127.0.0.1::8080'], 'networks': ['default', 'edge'],
                         'volumes': [f'{state}:/etc/packetsafari-proxy:ro']}}
    for slot in ['blue', 'green', 'other']:
        services[slot] = {'image': 'python:3.14-slim-bookworm', 'command': ['python', '/fixture.py'],
                          'environment': {'SLOT': slot, 'STREAM_DELAY': '0.5'}, 'volumes': [f'{fixture}:/fixture.py:ro']}
    compose = run / 'compose.json'
    compose.write_text(json.dumps({'services': services, 'networks': {'default': {'internal': True}, 'edge': {}}}))
    command = ['docker', 'compose', '-p', project, '-f', str(compose)]

    def dc(*args):
        return subprocess.run(command + list(args), capture_output=True, text=True, check=True, timeout=60).stdout.strip()

    events, samples = [], []
    stop = threading.Event()
    try:
        dc('up', '-d')
        containers = {s: dc('ps', '-q', s) for s in services}
        port = int(dc('port', 'proxy', '8080').rsplit(':', 1)[1])

        def connection():
            return closing(http.client.HTTPConnection('127.0.0.1', port, timeout=8))

        def request():
            with connection() as conn:
                conn.request('GET', '/')
                response = conn.getresponse()
                assert response.status == 200, response.status
                return json.loads(response.read())['slot']

        def deploy(slot, **kwargs):
            receipt = switch(state, containers['proxy'], containers[slot], port=8000, health_path='/ready', **kwargs)
            events.append({'case': slot, **receipt})
            return receipt

        # Let the fresh proxy master start before the first control request.
        for _ in range(50):
            result = docker('exec', containers['proxy'], 'wget', '-qO-', 'http://127.0.0.1:8099/generation', check=False)
            if result.returncode == 0:
                break
            time.sleep(.1)
        deploy('blue')
        assert request() == 'blue'

        def poll():
            while not stop.is_set():
                started = time.monotonic()
                try:
                    slot, error = request(), None
                except Exception as exc:
                    slot, error = None, str(exc)
                samples.append({'at': started, 'latency': time.monotonic() - started, 'slot': slot, 'error': error})
                stop.wait(.02)

        thread = threading.Thread(target=poll)
        thread.start()
        docker('exec', containers['green'], 'touch', '/tmp/unhealthy')
        before = (state / 'nginx.conf').read_text()
        try:
            deploy('green', ready_timeout=1)
            raise AssertionError('Unhealthy candidate accepted')
        except RuntimeError as exc:
            assert 'failed readiness' in str(exc)
        assert request() == 'blue'
        assert before == (state / 'nginx.conf').read_text()
        events.append({'case': 'unhealthy-candidate', 'status': 'rejected-old-serving'})
        docker('exec', containers['green'], 'rm', '/tmp/unhealthy')

        stream_started, upload_started, ws_started = threading.Event(), threading.Event(), threading.Event()
        payload = b'packet-evidence' * 32768

        def stream():
            with connection() as conn:
                conn.request('GET', '/stream')
                response = conn.getresponse()
                first = response.readline()
                assert first == b'data: blue:0\n', first
                stream_started.set()
                body = first + response.read()
                assert body.count(b'data: blue:') == 30
                return 'SSE complete'

        def upload():
            with connection() as conn:
                conn.putrequest('POST', '/upload')
                conn.putheader('Content-Length', str(len(payload)))
                conn.endheaders()
                for offset in range(0, len(payload), 8192):
                    conn.send(payload[offset:offset+8192])
                    upload_started.set()
                    time.sleep(.04)
                response = conn.getresponse()
                assert json.loads(response.read()) == {'slot': 'blue', 'sha256': hashlib.sha256(payload).hexdigest()}
                return 'upload hash matched'

        def websocket():
            with socket.create_connection(('127.0.0.1', port), timeout=8) as sock:
                sock.sendall(b'GET /ws HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n')
                stream = sock.makefile('rb')
                assert b'101' in stream.readline()
                while stream.readline() != b'\r\n':
                    pass
                for i in range(30):
                    header = stream.read(2)
                    assert len(header) == 2 and header[0] == 0x81, header
                    assert stream.read(header[1]) == f'blue:{i}'.encode()
                    ws_started.set()
                assert stream.read(4) == b'\x88\x02\x03\xe8'
                return 'WebSocket complete'

        with ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(fn) for fn in [stream, upload, websocket]]
            assert all(event.wait(5) for event in [stream_started, upload_started, ws_started])
            result = deploy('green', drain_timeout=0)
            assert result['status'] == 'draining', result
            assert request() == 'green'
            repeated = deploy('green', drain_timeout=0)
            assert repeated['generation'] == result['generation']
            assert repeated['status'] == 'draining'
            try:
                deploy('other', drain_timeout=0)
                raise AssertionError('Replaced a generation while it was draining')
            except ValueError as exc:
                assert 'still draining' in str(exc)
            # Returning to the exact retained instance is safe even while its
            # existing streams drain; replacing that instance remains forbidden.
            assert deploy('blue', drain_timeout=0)['status'] == 'draining'
            assert request() == 'blue'
            events.extend({'case': future.result(), 'status': 'passed'} for future in futures)
        # Existing keep-alive worker shutdown can complete just after the final byte.
        time.sleep(.3)
        deploy('blue')
        assert request() == 'blue'
        events.append({'case': 'rollback', 'status': 'passed'})
        stop.set()
        thread.join()
        assert not [s for s in samples if s['error']], samples
        # Restart is recovery qualification, explicitly outside the cutover sample window.
        dc('restart', 'proxy')
        port = int(dc('port', 'proxy', '8080').rsplit(':', 1)[1])
        for _ in range(50):
            try:
                if request() == 'blue':
                    break
            except Exception:
                pass
            time.sleep(.1)
        else:
            raise AssertionError('Proxy failed to recover persisted routing')
        events.append({'case': 'proxy-restart', 'status': 'passed'})
        report = {'status': 'passed', 'events': events, 'samples': len(samples), 'errors': [s for s in samples if s['error']],
                  'maxRequestSeconds': max(s['latency'] for s in samples), 'proxyImage': PROXY_IMAGE}
        (run / 'result.json').write_text(json.dumps(report, indent=2))
        print(json.dumps({'evidence': str(run), **report}, indent=2))
    finally:
        stop.set()
        if 'thread' in locals():
            thread.join(timeout=10)
        (run / 'requests.json').write_text(json.dumps(samples, indent=2))
        (run / 'docker.log').write_text(dc('logs', '--no-color'))
        dc('down')  # Only containers/network created by this unique test project; no volumes.


if __name__ == '__main__':
    main()
