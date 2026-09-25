"""The installed SaaS baseline must approve and route the built-in signup feed."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from packetsafari_onprem import operations


class SignupEmailEgressTest(unittest.TestCase):
    def test_rendered_feed_route_is_saas_only_and_idempotent(self):
        source = Path(__file__).resolve().parents[1]
        for profile, mode, expected in [('saas', 'shared_saas', True),
                                        ('onprem', 'onprem', False),
                                        ('saas', 'dedicated', False)]:
            with self.subTest(profile=profile, mode=mode), tempfile.TemporaryDirectory() as root:
                layout = operations.runtime_layout(root, root)
                operations.ensure_runtime_dirs(layout)
                shutil.copytree(source / 'templates/egress-config', layout.configuration_dir, dirs_exist_ok=True)
                layout.runtime_env_path.write_text(f'PACKETSAFARI_DEPLOYMENT_MODE={mode}\n')
                for _ in range(2):
                    operations._apply_profile_egress_overlay(layout, source, profile=profile)
                    operations._sync_intelligence_egress_config(layout)
                config = json.loads(layout.production_egress_allowlist_path.read_text())
                entries = [entry for entry in config['destinations'] if entry.get('host') == 'disposable.github.io']
                self.assertEqual(len(entries), int(expected))
                if expected:
                    self.assertEqual(entries[0]['purpose'], 'Security intelligence')
                    self.assertEqual(entries[0]['port'], 443)
                    self.assertEqual(entries[0]['owners'], ['worker'])
                self.assertEqual('        - "disposable.github.io"' in
                                 layout.production_ironproxy_config_path.read_text(), expected)


if __name__ == '__main__':
    unittest.main()
