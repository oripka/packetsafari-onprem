#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import platform as host_platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = Path.home() / "packetsafari-data"
APP_SERVICES = {
    "frontend": "frontend-production",
    "backend": "backend-production",
    "sharkd": "sharkd-production",
}
APP_DOCKERFILE_SERVICES = {
    "egress-ironproxy": "configuration/iron-proxy/Dockerfile",
    "egress-firewall": "configuration/egress-firewall/Dockerfile",
}
IMAGE_REPOSITORY_PREFIX = "packetsafari"
INFRA_IMAGES = {
    "redis": "redis/redis-stack-server:latest",
    "postgres": "postgres:16",
    "egress-dns": "coredns/coredns:1.11.3@sha256:9caabbf6238b189a65d0d6e6ac138de60d6a1c419e5a341fbbb7c78382559c6e",
}


def default_docker_platform() -> str:
    override = os.getenv("DOCKER_PLATFORM")
    if override:
        return override
    machine = host_platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "linux/arm64"
    return "linux/amd64"


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, cwd=cwd, env=env)


def capture(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(command, check=True, cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip()


def require_command(command: str) -> None:
    if not shutil.which(command):
        raise SystemExit(f"Required command not found: {command}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_app_version(app_root: Path) -> str:
    version_path = app_root / "configuration" / "app-version.json"
    payload = json.loads(version_path.read_text(encoding="utf-8"))
    version = str(payload.get("version") or "").strip()
    if not version:
        raise SystemExit(f"Missing version in {version_path}")
    return version


def required_onprem_env(app_root: Path) -> list[str]:
    registry_path = app_root / "configuration" / "env-registry.json"
    fallback = [
        "PACKETSAFARI_AUTH_JWT_SECRET_KEY",
        "REDIS_PASSWORD",
        "AI_AGENT_STREAM_TICKET_SECRET",
        "PACKETSAFARI_CAPTURE_SHARKD_JWT_SECRET",
    ]
    if not registry_path.exists():
        return fallback
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    required: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("required") is True and entry.get("lifecycle") == "bootstrap" and isinstance(entry.get("onPrem"), dict):
            key = str(entry.get("key") or "").strip()
            if key:
                required.append(key)
    return required or fallback


def git_value(app_root: Path, args: list[str]) -> str:
    try:
        return capture(["git", *args], cwd=app_root)
    except subprocess.CalledProcessError:
        return ""


def build_app_images(app_root: Path, version: str, *, platform: str, wireshark_cache_bust: str) -> dict[str, str]:
    images: dict[str, str] = {}
    docker_env = {**os.environ, "DOCKER_BUILDKIT": os.environ.get("DOCKER_BUILDKIT", "1")}
    prepare_args = [sys.executable, str(app_root / "scripts" / "prepare_wireshark_source.py")]
    if os.getenv("WIRESHARK_SHA"):
        prepare_args.extend(["--sha", str(os.environ["WIRESHARK_SHA"])])
    wireshark_sha = capture(prepare_args, cwd=app_root)
    if os.getenv("KEEP_WIRESHARK_SOURCE_ARCHIVE") != "1":
        atexit.register(
            subprocess.run,
            [sys.executable, str(app_root / "scripts" / "prepare_wireshark_source.py"), "--clean"],
            cwd=app_root,
            check=False,
        )
    for service, target in APP_SERVICES.items():
        image = f"{IMAGE_REPOSITORY_PREFIX}/{service}:{version}"
        print(f"Building {service} image: {image}")
        run(
            [
                "docker",
                "build",
                "--platform",
                platform,
                "--target",
                target,
                "--build-arg",
                "WIRESHARK_BUILD_REV=1",
                "--build-arg",
                f"WIRESHARK_CACHE_BUST={wireshark_cache_bust}",
                "--build-arg",
                f"WIRESHARK_SHA={wireshark_sha}",
                "-t",
                image,
                ".",
            ],
            cwd=app_root,
            env=docker_env,
        )
        images[service] = image
    for service, dockerfile in APP_DOCKERFILE_SERVICES.items():
        image = f"{IMAGE_REPOSITORY_PREFIX}/{service}:{version}"
        print(f"Building {service} image: {image}")
        run(
            [
                "docker",
                "build",
                "--platform",
                platform,
                "-f",
                dockerfile,
                "-t",
                image,
                ".",
            ],
            cwd=app_root,
            env=docker_env,
        )
        images[service] = image
    images["worker"] = images["backend"]
    return images


def prepare_infra_images(version: str, *, pull: bool) -> dict[str, str]:
    images: dict[str, str] = {}
    for service, source in INFRA_IMAGES.items():
        target = f"{IMAGE_REPOSITORY_PREFIX}/{service}:{version}"
        if pull:
            print(f"Pulling {source}")
            run(["docker", "image", "pull", source])
        run(["docker", "image", "tag", source, target])
        images[service] = target
    return images


def write_manifest(app_root: Path, output_dir: Path, version: str, channel: str, images: dict[str, str], *, platform: str) -> Path:
    tooling_version_path = REPO_ROOT / "VERSION"
    tooling_version = tooling_version_path.read_text(encoding="utf-8").strip() if tooling_version_path.exists() else "local"
    manifest = {
        "version": version,
        "channel": channel,
        "gitCommit": git_value(app_root, ["rev-parse", "HEAD"]),
        "gitBranch": git_value(app_root, ["branch", "--show-current"]),
        "builtAt": datetime.now(timezone.utc).isoformat(),
        "configSchemaVersion": 1,
        "platform": platform,
        "requiredEnv": required_onprem_env(app_root),
        "tooling": {
            "version": tooling_version,
            "minOpsVersion": tooling_version,
        },
        "images": images,
    }
    wireshark_metadata_path = app_root / ".packetsafari-build" / "wireshark-source.json"
    if wireshark_metadata_path.exists():
        wireshark_metadata = json.loads(wireshark_metadata_path.read_text(encoding="utf-8"))
        manifest["wiresharkSource"] = {
            key: wireshark_metadata.get(key)
            for key in ("sourceMode", "canonicalRef", "sha", "tree", "archiveSha256")
        }
    path = output_dir / "release-manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def ensure_rsa_key(private_key: Path, public_key: Path) -> None:
    private_key.parent.mkdir(parents=True, exist_ok=True)
    if not private_key.exists():
        run(["openssl", "genrsa", "-out", str(private_key), "3072"])
        private_key.chmod(0o600)
    run(["openssl", "rsa", "-in", str(private_key), "-pubout", "-out", str(public_key)])


def create_dev_license(output_dir: Path, key_dir: Path, version: str, channel: str, *, customer_email: str) -> tuple[Path, Path]:
    private_key = key_dir / "dev-license-private.pem"
    public_key = output_dir / "license-public.pem"
    ensure_rsa_key(private_key, public_key)
    token = output_dir / "license-token.json"
    run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "license_create.py"),
            "--private-key",
            str(private_key),
            "--customer-id",
            "local-dev",
            "--customer-email",
            customer_email,
            "--license-id",
            f"local-dev-{version}",
            "--deployment-id",
            f"local-dev-{version}",
            "--support-tier",
            "development",
            "--max-users",
            "25",
            "--max-agent-units-per-month",
            "1000",
            "--channel",
            channel,
            "--allowed-version",
            version,
            "--days",
            "30",
            "--notes",
            "Development-only license generated by scripts/build_local_release.py.",
            "--output",
            str(token),
        ]
    )
    return token, public_key


def create_onprem_archive(output_dir: Path) -> Path:
    archive_path = output_dir / "packetsafari-onprem.tar.gz"
    with tempfile.TemporaryDirectory(prefix="packetsafari-onprem-archive-") as temp_name:
        archive_root = Path(temp_name) / "packetsafari-onprem-local"
        shutil.copytree(
            REPO_ROOT,
            archive_root,
            ignore=shutil.ignore_patterns(
                ".git",
                ".guard",
                ".pytest_cache",
                ".venv",
                "__pycache__",
                "*.pyc",
                "*.egg-info",
                "build",
                "dist",
            ),
        )
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(archive_root, arcname=archive_root.name)
    return archive_path


def write_bootstrap_files(output_dir: Path) -> None:
    shutil.copy2(REPO_ROOT / "bootstrap.sh", output_dir / "bootstrap.sh")
    version_path = REPO_ROOT / "VERSION"
    archive_path = output_dir / "packetsafari-onprem.tar.gz"
    if version_path.exists():
        shutil.copy2(version_path, output_dir / "VERSION")
        version = version_path.read_text(encoding="utf-8").strip()
    else:
        (output_dir / "VERSION").write_text("local\n", encoding="utf-8")
        version = "local"
    manifest = {
        "schemaVersion": 1,
        "toolingVersion": version,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "files": {
            "packetsafari-onprem.tar.gz": {
                "sha256": sha256(archive_path) if archive_path.exists() else "",
            },
            "packetsafari_onprem/cli.py": {
                "sha256": sha256(REPO_ROOT / "packetsafari_onprem" / "cli.py"),
            },
        },
    }
    (output_dir / "bootstrap-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_install_notes(output_dir: Path, bundle_name: str, *, version: str, platform: str, dev_license: bool) -> Path:
    note = output_dir / "INSTALL.md"
    trust_flag = " \\\n    --allow-bundled-license-public-key" if dev_license else ""
    note.write_text(
        f"""# PacketSafari Local On-Prem Release {version}

This directory is self-contained for a local VM install test. It contains `{platform}` images, so the Ubuntu VM must use the same CPU architecture. Serve it from the Mac:

```bash
cd {output_dir}
python3 -m http.server 9000 --bind 0.0.0.0
```

On the Ubuntu Server VM, install Docker Engine and the Compose plugin first, then run:

```bash
export PACKETSAFARI_ONPREM_RAW_BASE=http://<mac-ip>:9000
export PACKETSAFARI_ONPREM_ARCHIVE_URL=http://<mac-ip>:9000/packetsafari-onprem.tar.gz
curl -fsSL http://<mac-ip>:9000/bootstrap.sh | sudo -E bash -s -- install \\
  --bundle http://<mac-ip>:9000/{bundle_name} \\
  --bundle-public-key http://<mac-ip>:9000/release-public.pem{trust_flag}
```

For authenticated downloads, set `PACKETSAFARI_ONPREM_BEARER_TOKEN` before bootstrap and pass
`--download-bearer-token` to `packetsafari-ops install` or `upgrade`.

The generated dev license and signing keys are for local validation only. Do not ship them to customers.
""",
        encoding="utf-8",
    )
    return note


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a locally hosted PacketSafari on-prem release directory.")
    parser.add_argument("--app-root", default=str(REPO_ROOT.parent / "packetsafari"))
    parser.add_argument("--version")
    parser.add_argument("--channel", default="local")
    parser.add_argument("--output-dir")
    parser.add_argument("--platform", default=default_docker_platform(), help="Image platform to build, defaults to native host architecture or DOCKER_PLATFORM.")
    parser.add_argument("--wireshark-cache-bust", default="local-release")
    parser.add_argument("--skip-build", action="store_true", help="Use existing packetsafari/* image tags.")
    parser.add_argument("--skip-infra-pull", action="store_true", help="Do not pull postgres/redis before tagging local copies.")
    parser.add_argument("--no-dev-license", action="store_true", help="Do not generate a development license token.")
    parser.add_argument("--customer-email", default="local-dev@packetsafari.com")
    parser.add_argument("--key-dir", help="Private signing key directory. Defaults outside the distributable output directory.")
    parser.add_argument("--split-size-mb", type=int, default=0)
    args = parser.parse_args()

    for command in ("docker", "openssl", "tar"):
        require_command(command)
    app_root = Path(args.app_root).expanduser().resolve()
    if not (app_root / "Dockerfile").exists():
        raise SystemExit(f"PacketSafari app Dockerfile not found under {app_root}")
    version = str(args.version or read_app_version(app_root)).strip()
    output_dir = Path(args.output_dir or (DEFAULT_DATA_ROOT / "releases" / "local" / version)).expanduser().resolve()
    key_dir = Path(args.key_dir or (DEFAULT_DATA_ROOT / "release-keys" / "local" / version)).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_build:
        images = {
            service: f"{IMAGE_REPOSITORY_PREFIX}/{service}:{version}"
            for service in APP_SERVICES
        }
        images["worker"] = images["backend"]
    else:
        images = build_app_images(app_root, version, platform=args.platform, wireshark_cache_bust=args.wireshark_cache_bust)
    images.update(prepare_infra_images(version, pull=not args.skip_infra_pull))

    manifest = write_manifest(app_root, output_dir, version, args.channel, images, platform=args.platform)
    notes = output_dir / "release-notes.md"
    if not notes.exists():
        notes.write_text(f"PacketSafari local on-prem release {version}.\n", encoding="utf-8")

    release_private_key = key_dir / "release-private.pem"
    release_public_key = output_dir / "release-public.pem"
    ensure_rsa_key(release_private_key, release_public_key)
    tooling_archive = create_onprem_archive(output_dir)

    bundle_args = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "build_offline_bundle.py"),
        "--manifest",
        str(manifest),
        "--output",
        str(output_dir / f"packetsafari-{version}-offline.tar.zst"),
        "--release-notes",
        str(notes),
        "--release-public-key",
        str(release_public_key),
        "--tooling-archive",
        str(tooling_archive),
        "--sign-key",
        str(release_private_key),
        "--no-pull",
    ]
    dev_license = not args.no_dev_license
    if dev_license:
        token, license_public_key = create_dev_license(output_dir, key_dir, version, args.channel, customer_email=args.customer_email)
        bundle_args.extend(["--license", str(token), "--license-public-key", str(license_public_key)])
    if args.split_size_mb:
        bundle_args.extend(["--split-size-mb", str(args.split_size_mb)])
    run(bundle_args)

    write_bootstrap_files(output_dir)
    install_notes = write_install_notes(output_dir, f"packetsafari-{version}-offline.tar.zst", version=version, platform=args.platform, dev_license=dev_license)

    print(json.dumps({
        "outputDir": str(output_dir),
        "bundle": str(output_dir / f"packetsafari-{version}-offline.tar.zst"),
        "manifest": str(manifest),
        "bootstrap": str(output_dir / "bootstrap.sh"),
        "installNotes": str(install_notes),
        "privateKeyDir": str(key_dir),
        "devLicenseIncluded": dev_license,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
