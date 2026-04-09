#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from license_common import b64url_encode, canonical_bytes, default_expiry, openssl_sign, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--customer-email", required=True)
    parser.add_argument("--license-id", required=True)
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--output", required=True)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    issued_at = datetime.now(timezone.utc)
    payload = {
        "customerId": args.customer_id,
        "customerEmail": args.customer_email,
        "licenseId": args.license_id,
        "issuedAt": issued_at.isoformat(),
        "expiresAt": (issued_at + timedelta(days=args.days)).isoformat(),
        "channel": args.channel,
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
