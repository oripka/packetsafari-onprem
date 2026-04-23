#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from license_common import b64url_decode, openssl_verify, read_token


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--public-key", required=True)
    args = parser.parse_args()

    token = read_token(Path(args.token))
    payload_bytes = b64url_decode(token["payload"])
    signature = b64url_decode(token["signature"])
    openssl_verify(Path(args.public_key), payload_bytes, signature)
    payload = json.loads(payload_bytes.decode("utf-8"))
    expires_at = str(payload.get("offline_expiry") or payload.get("offlineExpiry") or payload.get("expiresAt") or "").strip()
    if expires_at:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        if expiry < datetime.now(timezone.utc):
            raise SystemExit("License token has expired.")
    print(json.dumps({"verified": True, "payload": payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
