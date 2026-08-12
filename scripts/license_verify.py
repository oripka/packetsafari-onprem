#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from license_common import b64url_decode, openssl_verify, read_token

LICENSE_CLAIM_SCHEMA_VERSION = 1
REQUIRED_LICENSE_CLAIMS = (
    ("schema_version", "schemaVersion"),
    ("agent_enabled", "agentEnabled"),
    ("max_users", "maxUsers"),
    ("customer_id", "customerId"),
    ("deployment_id", "deploymentId", "licenseId"),
    ("support_tier", "supportTier"),
)
CANONICAL_AI_LIMIT_CLAIMS = (
    ("max_analysis_runs_per_month", "maxAnalysisRunsPerMonth"),
    ("max_quick_questions_per_month", "maxQuickQuestionsPerMonth"),
    ("max_prompt_coach_requests_per_month", "maxPromptCoachRequestsPerMonth"),
)


def _claim(payload: dict, *names: str):
    for name in names:
        if name in payload:
            return payload.get(name)
    return None


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _require_claims(payload: dict) -> None:
    missing = [names[0] for names in REQUIRED_LICENSE_CLAIMS if _blank(_claim(payload, *names))]
    if missing:
        raise SystemExit(f"License token is missing required claims: {', '.join(missing)}.")

    has_canonical_ai_limits = any(
        not _blank(_claim(payload, *names)) for names in CANONICAL_AI_LIMIT_CLAIMS
    )
    if has_canonical_ai_limits:
        missing_ai_limits = [names[0] for names in CANONICAL_AI_LIMIT_CLAIMS if _blank(_claim(payload, *names))]
    else:
        legacy_analysis_limit = _claim(payload, "max_agent_runs_per_month", "maxAgentRunsPerMonth")
        missing_ai_limits = [] if not _blank(legacy_analysis_limit) else ["max_analysis_runs_per_month"]
    if missing_ai_limits:
        raise SystemExit(f"License token is missing required claims: {', '.join(missing_ai_limits)}.")

    schema_version = _claim(payload, "schema_version", "schemaVersion")
    try:
        parsed_schema_version = int(schema_version)
    except (TypeError, ValueError):
        raise SystemExit("License token has invalid schema_version.")
    if parsed_schema_version != LICENSE_CLAIM_SCHEMA_VERSION:
        raise SystemExit(f"Unsupported license schema_version: {parsed_schema_version}.")

    limit_claims = {"max_users": ("max_users", "maxUsers")}
    if has_canonical_ai_limits:
        limit_claims.update({names[0]: names for names in CANONICAL_AI_LIMIT_CLAIMS})
    else:
        limit_claims["max_agent_runs_per_month"] = ("max_agent_runs_per_month", "maxAgentRunsPerMonth")
    for key, aliases in limit_claims.items():
        try:
            value = int(_claim(payload, *aliases))
        except (TypeError, ValueError):
            raise SystemExit(f"License token has invalid {key}.")
        if value < -1:
            raise SystemExit(f"License token has invalid {key}; use -1 for unlimited.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--public-key", required=True)
    args = parser.parse_args()

    token = read_token(Path(args.token))
    if str(token.get("alg") or "").strip() != "RS256":
        raise SystemExit("License token alg must be RS256.")
    payload_bytes = b64url_decode(token["payload"])
    signature = b64url_decode(token["signature"])
    openssl_verify(Path(args.public_key), payload_bytes, signature)
    payload = json.loads(payload_bytes.decode("utf-8"))
    _require_claims(payload)
    expires_at = str(_claim(payload, "offline_expiry", "offlineExpiry", "expiresAt") or "").strip()
    if not expires_at:
        raise SystemExit("License token is missing required claims: offline_expiry.")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise SystemExit("License token has invalid expiry.")
    if expiry < datetime.now(timezone.utc):
        raise SystemExit("License token has expired.")
    print(json.dumps({"verified": True, "payload": payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
