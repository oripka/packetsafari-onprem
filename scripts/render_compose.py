#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def image_ref(images: dict, key: str, default: str = "") -> str:
    value = images.get(key, default)
    if isinstance(value, dict):
        return str(value.get("image") or default)
    return str(value or default)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _profile_config(manifest: dict, profile: str) -> dict:
    profiles = manifest.get("deploymentProfiles")
    if isinstance(profiles, dict):
        config = profiles.get(profile)
        if isinstance(config, dict):
            return config
    return {}


def _omit_frontend(manifest: dict, profile: str) -> bool:
    if profile != "saas":
        return False
    config = _profile_config(manifest, profile)
    return _truthy(config.get("staticFrontend")) or _truthy(config.get("omitFrontendService"))


def _remove_service_block(compose_text: str, service: str) -> str:
    lines = compose_text.splitlines()
    output: list[str] = []
    skipping = False
    service_header = f"  {service}:"
    for line in lines:
        if line == service_header:
            skipping = True
            continue
        if skipping and line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            skipping = False
        if not skipping:
            output.append(line)
    return "\n".join(output) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-env-path", default="/opt/packetsafari/env/runtime.env")
    parser.add_argument("--host-runtime-root", default="/opt/packetsafari")
    parser.add_argument("--container-runtime-root", default="/storage/onprem")
    parser.add_argument("--profile", choices=["onprem", "saas"], default="onprem")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    template = Path(args.template).read_text(encoding="utf-8")
    images = manifest.get("images") or {}
    backend_image = image_ref(images, "backend")
    values = {
        "frontend_image": image_ref(images, "frontend"),
        "backend_image": backend_image,
        "worker_image": image_ref(images, "worker", backend_image),
        "redis_image": image_ref(images, "redis", "redis/redis-stack-server:latest"),
        "postgres_image": image_ref(images, "postgres", "postgres:16"),
        "sharkd_image": image_ref(images, "sharkd"),
        "egress_dns_image": image_ref(
            images,
            "egress-dns",
            "coredns/coredns:1.11.3@sha256:9caabbf6238b189a65d0d6e6ac138de60d6a1c419e5a341fbbb7c78382559c6e",
        ),
        "egress_ironproxy_image": image_ref(images, "egress-ironproxy"),
        "egress_firewall_image": image_ref(images, "egress-firewall"),
        "vector_image": image_ref(images, "vector", "timberio/vector:0.39.0-alpine"),
        "runtime_env_path": args.runtime_env_path,
        "host_runtime_root": args.host_runtime_root,
        "container_runtime_root": args.container_runtime_root,
    }
    required_image_keys = [
        "backend_image",
        "worker_image",
        "redis_image",
        "postgres_image",
        "sharkd_image",
        "egress_dns_image",
        "egress_ironproxy_image",
        "egress_firewall_image",
    ]
    if not _omit_frontend(manifest, args.profile):
        required_image_keys.insert(0, "frontend_image")
    missing = [key for key in required_image_keys if not values[key]]
    if missing:
        raise SystemExit(f"Manifest missing required image entries: {', '.join(missing)}")
    for key, value in values.items():
        template = template.replace(f"{{{{ {key} }}}}", str(value))
    if _omit_frontend(manifest, args.profile):
        template = _remove_service_block(template, "frontend")
    Path(args.output).write_text(template, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
