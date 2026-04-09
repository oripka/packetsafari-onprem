#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from license_common import b64url_decode, read_token


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    token = read_token(Path(args.token))
    payload = json.loads(b64url_decode(token["payload"]).decode("utf-8"))
    print(json.dumps({"token": token, "payload": payload}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
