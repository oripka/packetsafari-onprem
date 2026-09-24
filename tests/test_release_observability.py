"""Dependency-free checks: python3 -B -m unittest discover -s tests -p test_release_observability.py."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packetsafari_onprem import operations


class ReleaseObservabilityTest(unittest.TestCase):
    def test_dates(self):
        self.assertIn('02 Jan 2020 10:30:00 UTC', operations.release_time('2020-01-02T12:30:00+02:00')['display'])
        for value in (None, 'bad', '2026-01-01T00:00:00'):
            self.assertIsNone(operations.release_time(value)['timestamp'])
        self.assertIn('check clock', operations.release_time('2999-01-01T00:00:00Z')['display'])

    def test_progress_success_failure_and_quiet(self):
        for fail, quiet in ((False, False), (True, False), (False, True)):
            stderr, stdout = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                try:
                    with operations.update_progress(SimpleNamespace(quiet=quiet), 'Fetch'):
                        if fail:
                            raise RuntimeError('original error')
                except RuntimeError as error:
                    self.assertEqual(str(error), 'original error')
            self.assertEqual(stdout.getvalue(), '')
            if quiet:
                self.assertEqual(stderr.getvalue(), '')
            else:
                self.assertIn('Fetch...', stderr.getvalue())
                self.assertIn('failed after' if fail else 'done in', stderr.getvalue())

    def test_signed_payload_current_and_target_dates_and_plan(self):
        with tempfile.TemporaryDirectory() as root:
            layout = operations.runtime_layout(root, root)
            operations.ensure_runtime_dirs(layout)
            layout.release_manifest_path.write_text(json.dumps({'version': '10.0.1', 'builtAt': '2026-09-22T12:00:00Z'}))
            target = Path(root) / 'target.json'
            target.write_text(json.dumps({'version': '10.0.2', 'builtAt': '2026-09-23T12:00:00Z'}))
            args = SimpleNamespace(profile='saas', channel='stable', platform='linux-arm64', backup_mode='skip', _release_signature_verified=True)
            with patch.object(operations, 'sizing_state_status', return_value={}):
                payload = operations._update_check_payload(args, layout, target)
            self.assertEqual(payload['currentRelease']['timestamp'], '2026-09-22T12:00:00Z')
            self.assertEqual(payload['targetRelease']['timestamp'], '2026-09-23T12:00:00Z')
            self.assertEqual(payload['releaseSignature']['status'], 'verified')
            plan = operations.format_update_plan(payload, {})
            self.assertIn('22 Sep 2026', plan)
            self.assertIn('23 Sep 2026', plan)


if __name__ == '__main__':
    unittest.main()
