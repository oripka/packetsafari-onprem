from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location("license_verify", SCRIPTS_DIR / "license_verify.py")
license_verify = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(license_verify)


def _token(payload: dict, *, alg: str = "RS256") -> dict:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return {"alg": alg, "payload": encoded, "signature": "c2ln"}


def _valid_payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "agent_enabled": True,
        "max_users": 25,
        "max_agent_runs_per_month": 1000,
        "offline_expiry": "2099-01-01T00:00:00+00:00",
        "customer_id": "customer-1",
        "deployment_id": "deployment-1",
        "support_tier": "standard",
    }
    payload.update(overrides)
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class LicenseVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.original_argv = sys.argv[:]
        self.original_openssl_verify = license_verify.openssl_verify
        license_verify.openssl_verify = lambda *_args, **_kwargs: None

    def tearDown(self) -> None:
        sys.argv = self.original_argv
        license_verify.openssl_verify = self.original_openssl_verify
        self._tmpdir.cleanup()

    def _run_verify(self, token: dict) -> int:
        token_path = self.tmp_path / "license-token.json"
        public_key_path = self.tmp_path / "license-public.pem"
        _write_json(token_path, token)
        public_key_path.write_text("public", encoding="utf-8")
        sys.argv = ["license_verify.py", "--token", str(token_path), "--public-key", str(public_key_path)]
        with contextlib.redirect_stdout(io.StringIO()):
            return license_verify.main()

    def _assert_exits_with(self, token: dict, expected: str) -> None:
        with self.assertRaises(SystemExit) as raised:
            self._run_verify(token)
        self.assertIn(expected, str(raised.exception))

    def test_verify_accepts_complete_signed_claims(self):
        self.assertEqual(self._run_verify(_token(_valid_payload())), 0)

    def test_verify_rejects_missing_expiry(self):
        payload = _valid_payload()
        payload.pop("offline_expiry")

        self._assert_exits_with(_token(payload), "offline_expiry")

    def test_verify_rejects_missing_required_commercial_claim(self):
        payload = _valid_payload()
        payload.pop("max_agent_runs_per_month")

        self._assert_exits_with(_token(payload), "max_agent_runs_per_month")

    def test_verify_rejects_wrong_algorithm(self):
        self._assert_exits_with(_token(_valid_payload(), alg="none"), "RS256")

    def test_verify_rejects_invalid_quota(self):
        self._assert_exits_with(_token(_valid_payload(max_users=-2)), "max_users")


if __name__ == "__main__":
    unittest.main()
