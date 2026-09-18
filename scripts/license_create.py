#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from product_capabilities import license_products_error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from license_common import b64url_encode, canonical_bytes, openssl_sign, write_json


def monthly_limit(value: str) -> int:
    parsed = int(value)
    if parsed < -1:
        raise argparse.ArgumentTypeError("monthly limits must be -1 (unlimited), zero, or greater")
    return parsed


def analysis_cpu_slots(value: str) -> int:
    parsed = int(value)
    if parsed != -1 and parsed < 1:
        raise argparse.ArgumentTypeError("Use a positive number of heavy-analysis slots or -1 for unlimited.")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--customer-name", default="")
    parser.add_argument("--customer-email", required=True)
    parser.add_argument("--license-id", required=True)
    parser.add_argument("--deployment-id", default="")
    parser.add_argument("--support-tier", default="standard")
    parser.add_argument("--max-users", type=int, default=25)
    parser.add_argument("--max-host-cpus", type=analysis_cpu_slots, default=-1)
    parser.add_argument("--max-host-ram-gib", type=analysis_cpu_slots, default=-1)
    parser.add_argument("--max-analysis-cpu-slots", type=analysis_cpu_slots, default=-1,
                        help="Concurrent heavy-analysis slots; -1 is unlimited. Does not restrict RAM.")
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
    parser.add_argument("--edition", choices=["onprem", "onprem_airgapped"])
    parser.add_argument("--capabilities", help="Explicit JSON array of product capability IDs (schema 4)")
    args = parser.parse_args()

    issued_at = datetime.now(timezone.utc)
    expires_at = (issued_at + timedelta(days=args.days)).isoformat()
    deployment_id = str(args.deployment_id or args.license_id).strip()
    payload = {
        "schema_version": 3 if args.max_host_cpus != -1 or args.max_host_ram_gib != -1 else (2 if args.max_analysis_cpu_slots != -1 else 1),
        "max_host_cpus": args.max_host_cpus,
        "max_host_ram_gib": args.max_host_ram_gib,
        "agent_enabled": bool(args.agent_enabled),
        "max_users": int(args.max_users),
        "max_analysis_cpu_slots": args.max_analysis_cpu_slots,
        "max_analysis_runs_per_month": int(args.max_analysis_runs_per_month),
        "max_quick_questions_per_month": int(args.max_quick_questions_per_month),
        "max_prompt_coach_requests_per_month": int(args.max_prompt_coach_requests_per_month),
        "offline_expiry": expires_at,
        "customer_id": args.customer_id,
        "customer_name": args.customer_name,
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
    if args.edition is not None or args.capabilities is not None:
        try:
            payload.update(schema_version=4, edition=args.edition, capabilities=json.loads(args.capabilities or "null"))
        except ValueError:
            parser.error("Capabilities must be a JSON array")
        error = license_products_error(payload)
        if error:
            parser.error(error)
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
