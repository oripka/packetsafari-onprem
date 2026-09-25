"""Dependency-free checks: python3 -B -m unittest discover -s tests -p test_release_observability.py."""
import contextlib
import io
import json
import urllib.error
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

    def test_update_receipt_distinguishes_snapshot_from_data_backup(self):
        with tempfile.TemporaryDirectory() as root:
            layout = operations.runtime_layout(root, root)
            layout.release_manifest_path.parent.mkdir(parents=True)
            layout.release_manifest_path.write_text(json.dumps({
                'version': '10.0.2', 'builtAt': '2026-09-23T12:00:00Z',
                'gitCommit': 'abc', 'images': {'backend': 'repo@sha256:' + 'a' * 64},
                'embeddedSecurityContent': {'packages': [{'id': 'suricata-rules', 'type': 'suricata_rules_archive',
                                                          'generated_at': '2026-09-23T11:00:00Z'}],
                                            'unavailable_optional_package_types': ['ja4_compact']}}))
            args = SimpleNamespace(runtime_root=root, container_runtime_root=root, skip_health_check=False)
            result = operations._attach_update_summary(
                {'status': 'ok', 'version': '10.0.2', 'snapshot': '/snapshot/metadata'},
                {'app': {'currentVersion': '10.0.1', 'targetVersion': '10.0.2'},
                 'backupMode': 'skip', 'changedServices': ['backend']}, {}, args)
            receipt = result['deploymentReceipt']
            self.assertEqual(receipt['backup']['dataBackup'], 'not_captured')
            self.assertEqual(receipt['backup']['snapshotMetadata'], '/snapshot/metadata')
            self.assertIsNone(receipt['timing']['trafficUnavailableSeconds'])
            self.assertEqual(receipt['bundledInputs']['securityContent']['packages'][0]['status'], 'bundled')
            self.assertEqual(receipt['bundledInputs']['securityContent']['unavailableOptionalTypes'], ['ja4_compact'])
            self.assertEqual(json.loads(Path(result['deploymentReceiptPath']).read_text()), receipt)

    def test_verifier_detects_stale_origin_peer_and_running_image(self):
        from packetsafari_onprem import deployment_proxy
        with tempfile.TemporaryDirectory() as root:
            layout = operations.runtime_layout(root, root)
            layout.release_manifest_path.parent.mkdir(parents=True)
            image = 'repo@sha256:' + 'a' * 64
            layout.release_manifest_path.write_text(json.dumps({
                'version': '10.0.2', 'targetProfile': 'saas',
                'images': {name: image for name in ('backend', 'worker', 'sharkd', 'deployment-proxy')}}))
            rolling = layout.state_dir / 'rolling'
            rolling.mkdir()
            (rolling / 'stack.json').write_text(json.dumps({
                'fleetBases': {'backend': {'services': {
                    name: {'container_name': name} for name in ('backend', 'worker', 'sharkd')}}},
                'active': 'backend', 'proxyName': 'proxy'}))
            (rolling / 'proxy').mkdir()
            (rolling / 'proxy/state.json').write_text(json.dumps({
                'generation': 'gen1', 'ingressPolicy': {'mode': 'cloudfront-https',
                                                       'trustedCidrs': ['192.0.2.10/32']}}))
            args = SimpleNamespace(runtime_root=root, container_runtime_root=root, profile='saas',
                                   public_url='https://app.example.test', origin_peer_ip='192.0.2.11')
            error = urllib.error.HTTPError('https://app.example.test/sharkd', 401, 'Unauthorized', {}, None)
            doctor = {'ok': True, 'checks': [{'name': 'intelligence_updates', 'ok': True, 'feeds': [
                {'id': 'signup_email', 'enabled': True, 'status': 'error', 'updatedAt': ''}]}]}
            with patch.object(operations, 'doctor_deployment', return_value=doctor), \
                 patch.object(operations, '_http_probe', return_value={'ok': True, 'status': 200}), \
                 patch.object(operations.urllib.request, 'urlopen', side_effect=error), \
                 patch.object(deployment_proxy, 'generation', return_value='gen1'), \
                 patch.object(deployment_proxy, 'inspect', return_value={'Config': {'Image': image}}):
                result = operations.verify_deployment(args)
            self.assertFalse(result['ok'])
            self.assertEqual([check['name'] for check in result['checks'] if not check['ok']],
                             ['cloudfront_origin_peer'])
            self.assertEqual(result['feedWarnings'][0]['id'], 'signup_email')


if __name__ == '__main__':
    unittest.main()
