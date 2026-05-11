#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_ref(images: dict, key: str) -> str:
    value = images.get(key)
    if isinstance(value, dict):
        return str(value.get("image") or "")
    return str(value or "")


def save_image(image: str, output: Path, *, pull: bool) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if pull:
        subprocess.run(["docker", "image", "pull", image], check=True)
    raw = output.with_suffix("")
    subprocess.run(["docker", "image", "save", image, "-o", str(raw)], check=True)
    if shutil.which("zstd"):
        subprocess.run(["zstd", "-q", "-T0", "-f", str(raw), "-o", str(output)], check=True)
        raw.unlink(missing_ok=True)
        return output
    else:
        print(f"zstd not found; left uncompressed image archive at {raw}")
        return raw


def write_checksums(root: Path) -> Path:
    checksums = root / "checksums.txt"
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"checksums.txt", "checksums.txt.sig"}:
            continue
        lines.append(f"{sha256(path)}  {path.relative_to(root)}")
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksums


def sign_checksums(checksums: Path, private_key: Path) -> None:
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(checksums.with_suffix(".txt.sig")), str(checksums)],
        check=True,
    )


def create_archive(bundle_root: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = ["tar"]
    if output.name.endswith(".zst"):
        command.append("--zstd")
    command.extend(["-cf", str(output), "-C", str(bundle_root.parent), bundle_root.name])
    subprocess.run(command, check=True)


def split_file(path: Path, part_size_mb: int) -> list[Path]:
    part_size = part_size_mb * 1024 * 1024
    parts: list[Path] = []
    with path.open("rb") as source:
        index = 0
        while True:
            chunk = source.read(part_size)
            if not chunk:
                break
            suffix = chr(ord("a") + (index // 26)) + chr(ord("a") + (index % 26))
            part = path.with_name(f"{path.name}.part-{suffix}")
            part.write_bytes(chunk)
            parts.append(part)
            index += 1
    return parts


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a signed PacketSafari offline upgrade bundle.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--release-notes")
    parser.add_argument("--sbom-dir")
    parser.add_argument("--license", help="Optional license-token.json to include for fresh air-gapped installs.")
    parser.add_argument("--license-public-key", help="Optional license-public.pem to include for local/dev install bundles.")
    parser.add_argument("--release-public-key", help="Optional release-public.pem to include next to signed checksums.")
    parser.add_argument("--sign-key", help="Private key used to sign checksums.txt.")
    parser.add_argument("--no-pull", action="store_true", help="Use local Docker image tags without pulling them first.")
    parser.add_argument("--split-size-mb", type=int, default=0)
    args = parser.parse_args()

    manifest_path = Path(args.manifest).expanduser()
    output = Path(args.output).expanduser()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    images = manifest.get("images") or {}
    version = str(manifest.get("version") or "release")

    with tempfile.TemporaryDirectory(prefix=f"packetsafari-{version}-offline-") as tmp:
        root = Path(tmp) / f"packetsafari-{version}-offline"
        (root / "images").mkdir(parents=True)
        shutil.copy2(manifest_path, root / "release-manifest.json")
        if args.release_notes:
            shutil.copy2(Path(args.release_notes).expanduser(), root / "release-notes.md")
        if args.license:
            shutil.copy2(Path(args.license).expanduser(), root / "license-token.json")
        if args.license_public_key:
            shutil.copy2(Path(args.license_public_key).expanduser(), root / "license-public.pem")
        if args.release_public_key:
            shutil.copy2(Path(args.release_public_key).expanduser(), root / "release-public.pem")
        if args.sbom_dir:
            shutil.copytree(Path(args.sbom_dir).expanduser(), root / "sbom", dirs_exist_ok=True)

        image_metadata = []
        for service in sorted(images):
            if service == "worker" and image_ref(images, "worker") == image_ref(images, "backend"):
                continue
            image = image_ref(images, service)
            if image:
                archive = save_image(image, root / "images" / f"{service}.tar.zst", pull=not args.no_pull)
                image_metadata.append(
                    {
                        "service": service,
                        "image": image,
                        "archive": str(archive.relative_to(root)),
                        "sha256": sha256(archive),
                    }
                )
        (root / "image-metadata.json").write_text(
            json.dumps({"schemaVersion": 1, "images": image_metadata}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        checksums = write_checksums(root)
        if args.sign_key:
            sign_checksums(checksums, Path(args.sign_key).expanduser())
        create_archive(root, output)
        if args.split_size_mb:
            split_file(output, args.split_size_mb)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
