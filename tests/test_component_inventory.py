import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from packetsafari_onprem.component_inventory import collect, publish


class InventoryTests(unittest.TestCase):
    def test_projects_images_and_health_without_secrets(self):
        def run(args):
            if args[:2] == ['docker', 'inspect']:
                return json.dumps([{'Id': 'one', 'Image': 'sha256:image', 'Config': {
                    'Image': 'postgres:17', 'Env': ['PASSWORD=secret'],
                    'Labels': {'com.docker.compose.service': 'postgres'}},
                    'State': {'Running': True, 'Status': 'running', 'StartedAt': 'start', 'Health': {'Status': 'healthy', 'Log': ['secret']}}}])
            if args[:3] == ['docker', 'image', 'inspect']:
                return json.dumps([{'Id': 'sha256:image', 'Created': 'build', 'RepoDigests': ['repo@sha256:registry'],
                    'Config': {'Env': ['PG_VERSION=17.4', 'TOKEN=secret'], 'Labels': {'org.opencontainers.image.revision': 'abc'}}}])
            return 'one\n'
        result = collect(['docker', 'compose', '-p', 'project'], '0.2.1', run)
        row = result['components'][0]
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(row['version'], '17.4')
        self.assertEqual(row['health'], 'healthy')
        self.assertEqual(row['digests'], ['repo@sha256:registry'])
        self.assertNotIn('secret', json.dumps(result))

    def test_unavailable_is_not_empty_healthy(self):
        for run in (lambda args: '', lambda args: 'not-json', lambda args: 'id\n' * 129):
            self.assertEqual(collect(['compose'], '1', run)['status'], 'unavailable')

    def test_published_projection_is_readable_but_not_writable_by_api(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = SimpleNamespace(state_dir=Path(directory))
            (layout.state_dir / 'last-deployment-receipt.json').write_text(json.dumps({'recordedAt': 'time', 'outcome': 'ok', 'private': 'secret'}))
            with patch('packetsafari_onprem.component_inventory.collect', return_value={'status': 'observed'}):
                publish(layout, [], '1')
            path = layout.state_dir / 'component-inventory.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertNotIn('secret', path.read_text())
            self.assertEqual(json.loads(path.read_text())['deployment']['outcome'], 'ok')
