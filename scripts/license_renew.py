#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from license_common import b64url_decode, read_token


def _bool_claim(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
        "--customer-id", str(payload.get("customerId") or ""),
        "--customer-email", str(payload.get("customerEmail") or ""),
        "--license-id", str(payload.get("licenseId") or ""),
        "--deployment-id", str(payload.get("deployment_id") or payload.get("deploymentId") or payload.get("licenseId") or ""),
        "--support-tier", str(payload.get("support_tier") or payload.get("supportTier") or "standard"),
        "--max-users", str(payload.get("max_users") or payload.get("maxUsers") or 25),
        "--max-agent-runs-per-month", str(payload.get("max_agent_runs_per_month") or payload.get("maxAgentRunsPerMonth") or 1000),
        "--channel", str(payload.get("channel") or "stable"),
        "--days", str(args.days),
        "--output", args.output,
        "--notes", str(payload.get("notes") or ""),
    ]
    if not _bool_claim(payload.get("agent_enabled", payload.get("agentEnabled", True))):
        cmd.append("--no-agent")
    subprocess.run(cmd, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
