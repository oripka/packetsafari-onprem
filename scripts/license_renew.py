#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from license_common import b64url_decode, read_token


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
        "--channel", str(payload.get("channel") or "stable"),
        "--days", str(args.days),
        "--output", args.output,
        "--notes", str(payload.get("notes") or ""),
    ]
    subprocess.run(cmd, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
