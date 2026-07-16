#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from license_common import b64url_decode, read_token


DEFAULT_MAX_AGENT_UNITS_PER_MONTH = 5000


def _bool_claim(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _claim(payload: dict, *names: str, default=None):
    """Return the first populated claim without treating zero or false as absent."""
    for name in names:
        if name not in payload:
            continue
        value = payload[name]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return value
    return default


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    token = read_token(Path(args.token))
    payload = json.loads(b64url_decode(token["payload"]).decode("utf-8"))
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("license_create.py")),
        "--private-key", args.private_key,
        "--customer-id", str(_claim(payload, "customer_id", "customerId", default="")),
        "--customer-email", str(_claim(payload, "customer_email", "customerEmail", default="")),
        "--license-id", str(_claim(payload, "license_id", "licenseId", default="")),
        "--deployment-id", str(_claim(payload, "deployment_id", "deploymentId", "license_id", "licenseId", default="")),
        "--support-tier", str(_claim(payload, "support_tier", "supportTier", default="standard")),
        "--max-users", str(_claim(payload, "max_users", "maxUsers", default=25)),
        "--max-agent-units-per-month", str(
            _claim(
                payload,
                "max_agent_runs_per_month",
                "maxAgentRunsPerMonth",
                default=DEFAULT_MAX_AGENT_UNITS_PER_MONTH,
            )
        ),
        "--channel", str(_claim(payload, "channel", default="stable")),
        "--days", str(args.days),
        "--output", args.output,
        "--notes", str(_claim(payload, "notes", default="")),
    ]
    for allowed_version in _claim(payload, "allowed_versions", "allowedVersions", default=[]):
        normalized = str(allowed_version).strip()
        if normalized:
            cmd.extend(["--allowed-version", normalized])
    if not _bool_claim(_claim(payload, "agent_enabled", "agentEnabled", default=True)):
        cmd.append("--no-agent")
    subprocess.run(cmd, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
