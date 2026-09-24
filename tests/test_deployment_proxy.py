import tempfile
import fcntl
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

from packetsafari_onprem import deployment_proxy as proxy


class ConfigurationTests(unittest.TestCase):
    def test_invalid_trust_policy_rejected(self):
        for policy in [{}, {'mode': 'forwarded', 'trustedCidrs': []},
                       {'mode': 'forwarded', 'trustedCidrs': ['0.0.0.0/0']},
                       {'mode': 'forwarded', 'trustedCidrs': ['127.0.0.1; return 200;']},
                       {'mode': 'cloudfront-https', 'trustedCidrs': ['192.0.2.1/32']},
                       {'mode': 'direct', 'trustedCidrs': ['192.0.2.1/32']}]:
            with self.assertRaises(ValueError):
                proxy.ingress_policy(policy)

    def test_trust_persisted_at_initialization(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            policy = {'mode': 'forwarded', 'trustedCidrs': ['192.0.2.1/32']}
            proxy.initialize(path, policy)
            self.assertEqual(json.loads((path/'state.json').read_text())['ingressPolicy'], proxy.ingress_policy(policy))

    def test_streaming_and_no_automatic_retries(self):
        config = proxy.configuration('172.20.0.20:80', 'abc')
        for option in ['proxy_request_buffering off;', 'proxy_buffering off;',
                       'proxy_next_upstream off;', 'proxy_set_header Upgrade $http_upgrade;']:
            self.assertIn(option, config)
        self.assertNotIn('worker_shutdown_timeout', config)

    def test_configuration_injection_rejected(self):
        for endpoint in ['backend; return 200;', 'http://backend:80', 'backend:80/path']:
            with self.assertRaises(ValueError):
                proxy.configuration(endpoint, 'abc')

    def test_init_preserves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            proxy.initialize(path)
            before = (path / 'nginx.conf').read_text()
            with self.assertRaises(ValueError):
                proxy.initialize(path)
            self.assertEqual(before, (path / 'nginx.conf').read_text())

    def test_shared_namespace_address(self):
        network = {'NetworkSettings': {'Networks': {'dev': {'IPAddress': '172.20.0.20'}}}}
        target = {'State': {'Running': True}, 'HostConfig': {'NetworkMode': 'container:backend'}}
        with patch.object(proxy, 'inspect', return_value=network):
            self.assertEqual(proxy.address(network, target, 18081), '172.20.0.20:18081')

    def test_ambiguous_network_rejected(self):
        info = {'State': {'Running': True}, 'HostConfig': {'NetworkMode': 'bridge'},
                'NetworkSettings': {'Networks': {'a': {}, 'b': {}}}}
        with self.assertRaises(ValueError):
            proxy.address(info, info, 80)

    def test_config_drift_fails_before_docker(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            proxy.initialize(path)
            (path / 'nginx.conf').write_text('changed')
            with patch.object(proxy, 'docker') as docker:
                with self.assertRaisesRegex(ValueError, 'outside the cutover'):
                    proxy.switch(path, 'proxy', 'green')
                docker.assert_not_called()

    def test_concurrent_operation_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            proxy.initialize(path)
            with (path / 'lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    proxy.switch(path, 'proxy', 'green')

    def test_failed_reload_restores_restart_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            proxy.initialize(path)
            before = (path / 'nginx.conf').read_text()
            info = {'Id': 'green', 'Image': 'sha256:test',
                    'State': {'Running': True, 'StartedAt': 'now'},
                    'HostConfig': {'NetworkMode': 'bridge'},
                    'NetworkSettings': {'Networks': {'dev': {'IPAddress': '172.20.0.20'}}},
                    'Mounts': [{'Destination': '/etc/packetsafari-proxy', 'Source': str(path)}]}

            def command(*args, **kwargs):
                if 'reload' in args and kwargs.get('check', True):
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0, '', 'HTTP/1.1 200 OK')

            with patch.object(proxy, 'inspect', return_value=info), \
                 patch.object(proxy, 'generation', return_value='unconfigured'), \
                 patch.object(proxy, 'workers', return_value={'42'}), \
                 patch.object(proxy, 'docker', side_effect=command):
                with self.assertRaises(subprocess.CalledProcessError):
                    proxy.switch(path, 'proxy', 'green')
            self.assertEqual(before, (path / 'nginx.conf').read_text())
            self.assertIn('unconfigured', (path / 'state.json').read_text())


if __name__ == '__main__':
    unittest.main()
