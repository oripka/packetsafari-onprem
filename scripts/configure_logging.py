#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


DEFAULTS = {
    "AUDIT_LOG_ENABLED": "true",
    "AUDIT_LOG_PERSIST": "true",
    "AUDIT_RETENTION_DAYS": "365",
    "AUDIT_FORWARDING_MODE": "stdout_json",
    "AUDIT_FORWARDER_TYPE": "none",
}


def _normalize_bool(value: str | bool | None, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    return "true" if str(value).strip().lower() in {"1", "true", "yes", "y", "on"} else "false"


def _prompt_bool(label: str, default: str) -> str:
    suffix = "Y/n" if default == "true" else "y/N"
    while True:
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return "true"
        if raw in {"n", "no"}:
            return "false"


def _prompt_choice(label: str, options: list[str], default: str) -> str:
    prompt = "/".join(options)
    while True:
        raw = input(f"{label} [{default}] ({prompt}): ").strip().lower()
        if not raw:
            return default
        if raw in options:
            return raw


def _prompt_int(label: str, default: str) -> str:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return str(max(1, int(raw)))
        except Exception:
            continue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--audit-log-enabled")
    parser.add_argument("--audit-log-persist")
    parser.add_argument("--audit-retention-days")
    parser.add_argument("--audit-forwarding-mode")
    parser.add_argument("--audit-forwarder-type")
    args = parser.parse_args()

    values = dict(DEFAULTS)
    if args.non_interactive:
        values["AUDIT_LOG_ENABLED"] = _normalize_bool(args.audit_log_enabled, default=values["AUDIT_LOG_ENABLED"])
        values["AUDIT_LOG_PERSIST"] = _normalize_bool(args.audit_log_persist, default=values["AUDIT_LOG_PERSIST"])
        if args.audit_retention_days:
            values["AUDIT_RETENTION_DAYS"] = str(max(1, int(args.audit_retention_days)))
        if args.audit_forwarding_mode:
            values["AUDIT_FORWARDING_MODE"] = str(args.audit_forwarding_mode).strip().lower()
        if args.audit_forwarder_type:
            values["AUDIT_FORWARDER_TYPE"] = str(args.audit_forwarder_type).strip().lower()
    else:
        print("PacketSafari audit logging configuration")
        values["AUDIT_LOG_ENABLED"] = _prompt_bool("Enable audit logging", values["AUDIT_LOG_ENABLED"])
        values["AUDIT_LOG_PERSIST"] = _prompt_bool("Persist audit events in SQL", values["AUDIT_LOG_PERSIST"])
        values["AUDIT_RETENTION_DAYS"] = _prompt_int("Audit retention in days", values["AUDIT_RETENTION_DAYS"])
        values["AUDIT_FORWARDING_MODE"] = _prompt_choice(
            "Forwarding mode",
            ["stdout_json", "forwarder_profile", "custom_driver"],
            values["AUDIT_FORWARDING_MODE"],
        )
        if values["AUDIT_FORWARDING_MODE"] == "forwarder_profile":
            values["AUDIT_FORWARDER_TYPE"] = _prompt_choice(
                "Forwarder type",
                ["splunk", "elasticsearch", "opensearch", "loki", "syslog"],
                "splunk",
            )
        else:
            values["AUDIT_FORWARDER_TYPE"] = "none"

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{key}="{value}"' for key, value in values.items()]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
