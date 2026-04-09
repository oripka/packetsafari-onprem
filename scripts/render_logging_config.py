#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        text = value.strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        values[key.strip()] = text
    return values


def _sink_config(forwarder_type: str) -> str:
    if forwarder_type == "splunk":
        return """
[sinks.out]
type = "splunk_hec_logs"
inputs = ["audit_only"]
endpoint = "${SPLUNK_HEC_URL}"
token = "${SPLUNK_HEC_TOKEN}"
encoding.codec = "json"
host_key = "host"
index = "${SPLUNK_HEC_INDEX:-packetsafari}"
"""
    if forwarder_type in {"elasticsearch", "opensearch"}:
        return """
[sinks.out]
type = "elasticsearch"
inputs = ["audit_only"]
endpoints = ["${AUDIT_ELASTICSEARCH_ENDPOINT}"]
mode = "bulk"
compression = "gzip"
api_version = "v8"
[sinks.out.bulk]
index = "${AUDIT_ELASTICSEARCH_INDEX:-packetsafari-audit}"
[sinks.out.encoding]
codec = "json"
"""
    if forwarder_type == "loki":
        return """
[sinks.out]
type = "loki"
inputs = ["audit_only"]
endpoint = "${AUDIT_LOKI_ENDPOINT}"
encoding.codec = "json"
[sinks.out.labels]
app = "packetsafari"
stream = "audit"
"""
    if forwarder_type == "syslog":
        return """
[sinks.out]
type = "socket"
inputs = ["audit_only"]
mode = "tcp"
address = "${AUDIT_SYSLOG_ADDRESS}"
encoding.codec = "json"
"""
    return """
[sinks.out]
type = "console"
inputs = ["audit_only"]
target = "stdout"
encoding.codec = "json"
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-env", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    env_values = _read_env(Path(args.runtime_env))
    forwarder_type = str(env_values.get("AUDIT_FORWARDER_TYPE") or "none").strip().lower()

    config = f"""
[sources.docker]
type = "docker_logs"
docker_host = "unix:///var/run/docker.sock"
include_containers = ["packetsafari-backend", "packetsafari-worker", "packetsafari-securityworker"]

[transforms.audit_only]
type = "filter"
inputs = ["docker"]
condition = '.label."com.docker.compose.service" == "backend" || .label."com.docker.compose.service" == "worker" || .label."com.docker.compose.service" == "securityworker"'

{_sink_config(forwarder_type).strip()}
""".strip() + "\n"

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(config, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
