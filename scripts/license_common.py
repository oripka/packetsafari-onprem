#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    text = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(text.encode("ascii"))


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def iso_now() -> datetime:
    return datetime.now(timezone.utc)


def default_expiry(days: int = 365) -> str:
    return (iso_now() + timedelta(days=days)).isoformat()


def openssl_sign(private_key: Path, payload: bytes) -> bytes:
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key)],
        input=payload,
        check=True,
        capture_output=True,
    )
    return proc.stdout


def openssl_verify(public_key: Path, payload: bytes, signature: bytes) -> None:
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(signature)
        sig_path = Path(handle.name)
    try:
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public_key), "-signature", str(sig_path)],
            input=payload,
            capture_output=True,
            text=False,
        )
        if proc.returncode != 0:
            raise SystemExit("Signature verification failed.")
    finally:
        sig_path.unlink(missing_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_token(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
