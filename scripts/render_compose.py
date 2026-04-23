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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-env-path", default="/opt/packetsafari/env/runtime.env")
    parser.add_argument("--host-runtime-root", default="/opt/packetsafari")
    parser.add_argument("--container-runtime-root", default="/storage/onprem")
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
        "es_image": image_ref(images, "es01", "docker.elastic.co/elasticsearch/elasticsearch:7.17.8"),
        "sharkd_image": image_ref(images, "sharkd"),
        "vector_image": image_ref(images, "vector", "timberio/vector:0.39.0-alpine"),
        "runtime_env_path": args.runtime_env_path,
        "host_runtime_root": args.host_runtime_root,
        "container_runtime_root": args.container_runtime_root,
    }
    missing = [key for key in ("frontend_image", "backend_image", "worker_image", "sharkd_image") if not values[key]]
    if missing:
        raise SystemExit(f"Manifest missing required image entries: {', '.join(missing)}")
    for key, value in values.items():
        template = template.replace(f"{{{{ {key} }}}}", str(value))
    Path(args.output).write_text(template, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
