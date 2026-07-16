from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


license_common = _load_script("license_common")
license_create = _load_script("license_create")
license_renew = _load_script("license_renew")


class LicenseCreateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _issue(self, *quota_args: str) -> dict:
        output = self.tmp_path / f"license-{len(list(self.tmp_path.iterdir()))}.json"
        argv = [
            "license_create.py",
            "--private-key",
            str(self.tmp_path / "private.pem"),
            "--customer-id",
            "customer-1",
            "--customer-email",
            "security@example.com",
            "--license-id",
            "license-1",
            *quota_args,
            "--output",
            str(output),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(license_create, "openssl_sign", return_value=b"signature"),
        ):
            self.assertEqual(license_create.main(), 0)
        token = json.loads(output.read_text(encoding="utf-8"))
        return json.loads(license_common.b64url_decode(token["payload"]).decode("utf-8"))

    def test_default_is_five_thousand_weighted_units(self):
        payload = self._issue()

        self.assertEqual(payload["max_agent_runs_per_month"], 5000)

    def test_preferred_and_legacy_options_write_the_legacy_claim(self):
        for option, expected in (
            ("--max-agent-units-per-month", 4321),
            ("--max-agent-runs-per-month", 1234),
        ):
            with self.subTest(option=option):
                payload = self._issue(option, str(expected))
                self.assertEqual(payload["max_agent_runs_per_month"], expected)


class LicenseRenewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _renew_command(self, payload: dict) -> list[str]:
        token_path = self.tmp_path / "license.json"
        token_path.write_text(
            json.dumps(
                {
                    "alg": "RS256",
                    "payload": license_common.b64url_encode(license_common.canonical_bytes(payload)),
                    "signature": "c2ln",
                }
            ),
            encoding="utf-8",
        )
        argv = [
            "license_renew.py",
            "--token",
            str(token_path),
            "--private-key",
            str(self.tmp_path / "private.pem"),
            "--output",
            str(self.tmp_path / "renewed.json"),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(license_renew.subprocess, "run") as run,
        ):
            self.assertEqual(license_renew.main(), 0)
        return list(run.call_args.args[0])

    def test_preserves_zero_quotas_allowed_versions_and_disabled_agent(self):
        cmd = self._renew_command(
            {
                "customer_id": "customer-1",
                "customerEmail": "security@example.com",
                "licenseId": "license-1",
                "deployment_id": "deployment-1",
                "support_tier": "enterprise",
                "max_users": 0,
                "max_agent_runs_per_month": 0,
                "agent_enabled": False,
                "channel": "stable",
                "allowed_versions": ["10.0.1", "10.0.2"],
            }
        )

        self.assertEqual(cmd[cmd.index("--max-users") + 1], "0")
        self.assertEqual(cmd[cmd.index("--max-agent-units-per-month") + 1], "0")
        self.assertNotIn("--max-agent-runs-per-month", cmd)
        self.assertIn("--no-agent", cmd)
        allowed_versions = [
            cmd[index + 1]
            for index, value in enumerate(cmd)
            if value == "--allowed-version"
        ]
        self.assertEqual(allowed_versions, ["10.0.1", "10.0.2"])

    def test_missing_agent_allowance_uses_enterprise_default(self):
        cmd = self._renew_command(
            {
                "customerId": "customer-1",
                "customerEmail": "security@example.com",
                "licenseId": "license-1",
            }
        )

        self.assertEqual(cmd[cmd.index("--max-agent-units-per-month") + 1], "5000")


if __name__ == "__main__":
    unittest.main()
