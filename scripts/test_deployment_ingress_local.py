#!/usr/bin/env python3
"""Isolated Docker ingress tests; no credentials or production access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packetsafari_onprem import deployment_proxy as proxy


def main():
    root = Path(os.environ.get('PACKETSAFARI_DATA_ROOT', '/Users/otr/packetsafari-data')) / 'runs/deployment-proxy' / ('ingress-' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    fixture = root / 'echo.py'
    fixture.write_text('''from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  body=json.dumps(dict(self.headers)).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
''')
    services = {'upstream': {'image': 'python:3.14-slim-bookworm', 'command': ['python', '/echo.py'], 'volumes': [f'{fixture}:/echo.py:ro']}}
    for name in ['trusted', 'stranger']:
        services[name] = {'image': 'python:3.14-slim-bookworm', 'command': ['sleep', '300']}
    inner_config = root / 'inner.conf'
    inner_config.touch()
    services['upstream']['networks'] = {'default': {'aliases': ['agent-stream-gateway']}}
    services['inner'] = {'image': proxy.PROXY_IMAGE, 'command': ['nginx', '-g', 'daemon off;', '-c', '/probe/inner.conf'],
                         'volumes': [f'{root}:/probe:ro']}
    for mode in ['direct', 'forwarded', 'cloudfront-https']:
        (root / mode).mkdir()
        services[mode] = {'image': proxy.PROXY_IMAGE, 'command': ['nginx', '-g', 'daemon off;', '-c', '/etc/packetsafari-proxy/nginx.conf'],
                          'volumes': [f'{root / mode}:/etc/packetsafari-proxy:ro']}
    compose = root / 'compose.json'
    compose.write_text(json.dumps({'services': services, 'networks': {'default': {'internal': True}}}))
    command = ['docker', 'compose', '-p', 'ps-ingress-' + root.name[-8:], '-f', str(compose)]
    def dc(*args):
        return subprocess.run([*command, *args], check=True, text=True, capture_output=True, timeout=60).stdout.strip()
    checks = []
    try:
        dc('up', '-d', 'upstream', 'trusted', 'stranger')
        clients = {name: dc('ps', '-q', name) for name in ['trusted', 'stranger']}
        ips = {name: next(iter(proxy.inspect(cid)['NetworkSettings']['Networks'].values()))['IPAddress'] for name, cid in clients.items()}
        site = (Path(__file__).resolve().parents[2] / 'packetsafari/backend/config/nginx.conf').read_text()
        site = site.replace('__PACKETSAFARI_CLIENT_MAX_BODY_SIZE__', '12G').replace('172.20.0.26', ips['trusted'])
        site = site.replace('__PACKETSAFARI_AGENT_GATEWAY_HOST__', 'agent-stream-gateway')
        site = site.replace('include uwsgi_params;', 'include /etc/nginx/uwsgi_params;').replace(':8091', ':8000')
        inner_config.write_text('events {}\nhttp {\n' + site + '\n}\n')
        for mode in ['direct', 'forwarded', 'cloudfront-https']:
            policy = {'mode': mode, 'trustedCidrs': [] if mode == 'direct' else [ips['trusted'] + '/32'], 'viewerHttpsOnly': mode == 'cloudfront-https'}
            proxy.initialize(root / mode, policy)
        dc('up', '-d', 'direct', 'forwarded', 'cloudfront-https', 'inner')
        for mode in ['direct', 'forwarded', 'cloudfront-https']:
            controller = dc('ps', '-q', mode)
            for _ in range(50):
                try:
                    proxy.generation(controller)
                    break
                except subprocess.CalledProcessError:
                    time.sleep(.1)
            proxy.switch(root / mode, controller, dc('ps', '-q', 'upstream'), port=8000, health_path='/ready')
            assert json.loads((root / mode / 'state.json').read_text())['ingressPolicy']['mode'] == mode

        def request(client, mode, headers, port=8080, path='/'):
            script = "import http.client,json,sys;c=http.client.HTTPConnection(sys.argv[1],int(sys.argv[3]));c.request('GET',sys.argv[4],headers=json.loads(sys.argv[2]));r=c.getresponse();b=r.read();print(json.dumps({'status':r.status,'headers':json.loads(b) if r.status==200 else {}}))"
            return json.loads(proxy.docker('exec', clients[client], 'python', '-c', script, mode, json.dumps(headers), str(port), path).stdout)
        hostile = {'Host': 'app.example:8080', 'X-Forwarded-For': '198.51.100.99', 'X-Forwarded-Proto': 'https',
                   'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Port': '443', 'Forwarded': 'for=evil', 'CF-Connecting-IP': '198.51.100.88'}
        for mode in ['direct', 'forwarded', 'cloudfront-https']:
            result = request('stranger', mode, hostile)
            assert result['status'] == 200, result
            headers = result['headers']
            assert headers['X-Forwarded-For'] == ips['stranger'], headers
            assert headers['X-Forwarded-Proto'] == 'http', headers
            assert headers['X-Forwarded-Host'] == 'app.example:8080', headers
            assert not set(['Forwarded', 'CF-Connecting-IP', 'X-Forwarded-Port']) & set(headers), headers
            checks.append(f'{mode}: untrusted forwarding spoof rejected')
        for mode in ['forwarded', 'cloudfront-https']:
            headers = request('trusted', mode, {**hostile, 'X-Forwarded-For': '198.51.100.99, 203.0.113.7'})['headers']
            assert headers['X-Forwarded-For'] == '203.0.113.7' and headers['X-Forwarded-Proto'] == 'https', headers
            ipv6 = request('trusted', mode, {**hostile, 'X-Forwarded-For': '2001:db8::7'})['headers']
            assert ipv6['X-Forwarded-For'] == '2001:db8::7', ipv6
            assert request('trusted', mode, {'X-Forwarded-Proto': 'https'})['status'] == 400
            assert request('trusted', mode, {**hostile, 'X-Forwarded-For': 'not-an-address'})['status'] == 400
            checks.append(f'{mode}: trusted IPv4/IPv6 and last-hop selection; missing address fails closed')
        assert request('trusted', 'forwarded', {**hostile, 'X-Forwarded-Proto': 'https,http'})['status'] == 400
        assert request('trusted', 'forwarded', {'X-Forwarded-For': '203.0.113.7'})['status'] == 400
        assert request('trusted', 'forwarded', {**hostile, 'X-Forwarded-Proto': 'http'})['headers']['X-Forwarded-Proto'] == 'http'
        assert request('trusted', 'cloudfront-https', {**hostile, 'X-Forwarded-Proto': 'http'})['headers']['X-Forwarded-Proto'] == 'https'
        checks.append('trusted scheme validation and CloudFront viewer HTTPS enforcement')
        headers = request('trusted', 'inner', {**hostile, 'X-Forwarded-For': '203.0.113.7'}, 80, '/api/v2/ai/agent/stream')['headers']
        assert headers['X-Forwarded-Proto'] == 'https' and headers['X-Forwarded-For'] == '203.0.113.7', headers
        headers = request('stranger', 'inner', hostile, 80, '/api/v2/ai/agent/stream')['headers']
        assert headers['X-Forwarded-Proto'] == 'http' and headers['X-Forwarded-For'].endswith(ips['stranger']), headers
        checks.append('inner Agent gateway preserves normalized managed-proxy metadata; legacy peers cannot override scheme')
        report = {'passed': True, 'checks': checks, 'evidence': str(root)}
        (root / 'result.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        (root / 'docker.log').write_text(dc('logs', '--no-color'))
        dc('down')


if __name__ == '__main__':
    main()
