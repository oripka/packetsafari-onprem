#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from license_common import b64url_encode, canonical_bytes, openssl_sign, write_json


def monthly_limit(value: str) -> int:
    parsed = int(value)
    if parsed < -1:
        raise argparse.ArgumentTypeError("monthly limits must be -1 (unlimited), zero, or greater")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--customer-email", required=True)
    parser.add_argument("--license-id", required=True)
    parser.add_argument("--deployment-id", default="")
    parser.add_argument("--support-tier", default="standard")
    parser.add_argument("--max-users", type=int, default=25)
    parser.add_argument(
        "--max-analysis-runs-per-month",
        type=monthly_limit,
        required=True,
        help="Deployment-wide completed full investigations per calendar month; -1 is unlimited.",
    )
    parser.add_argument(
        "--max-quick-questions-per-month",
        type=monthly_limit,
        required=True,
        help="Deployment-wide Copilot and lightweight Agent-tab questions per calendar month; -1 is unlimited.",
    )
    parser.add_argument(
        "--max-prompt-coach-requests-per-month",
        type=monthly_limit,
        required=True,
        help="Deployment-wide Prompt Coach requests per calendar month; -1 is unlimited.",
    )
    parser.add_argument("--agent-enabled", dest="agent_enabled", action="store_true", default=True)
    parser.add_argument("--no-agent", dest="agent_enabled", action="store_false")
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--allowed-version", dest="allowed_versions", action="append", default=[])
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--output", required=True)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    issued_at = datetime.now(timezone.utc)
    expires_at = (issued_at + timedelta(days=args.days)).isoformat()
    deployment_id = str(args.deployment_id or args.license_id).strip()
    payload = {
        "schema_version": 1,
        "agent_enabled": bool(args.agent_enabled),
        "max_users": int(args.max_users),
        "max_analysis_runs_per_month": int(args.max_analysis_runs_per_month),
        "max_quick_questions_per_month": int(args.max_quick_questions_per_month),
        "max_prompt_coach_requests_per_month": int(args.max_prompt_coach_requests_per_month),
        "offline_expiry": expires_at,
        "customer_id": args.customer_id,
        "deployment_id": deployment_id,
        "support_tier": str(args.support_tier or "standard").strip().lower(),
        "customerId": args.customer_id,
        "customerEmail": args.customer_email,
        "licenseId": args.license_id,
        "deploymentId": deployment_id,
        "issuedAt": issued_at.isoformat(),
        "expiresAt": expires_at,
        "channel": args.channel,
        "allowed_versions": [str(item).strip() for item in args.allowed_versions if str(item).strip()],
        "notes": args.notes,
    }
    payload_bytes = canonical_bytes(payload)
    signature = openssl_sign(Path(args.private_key), payload_bytes)
    token = {
      "alg": "RS256",
      "payload": b64url_encode(payload_bytes),
      "signature": b64url_encode(signature),
    }
    write_json(Path(args.output), token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
