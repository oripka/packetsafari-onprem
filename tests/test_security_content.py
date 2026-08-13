from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from packetsafari_onprem import cli, operations


def _signed_pack(tmp_path: Path) -> tuple[Path, Path]:
    pack = tmp_path / "pack"
    package_dir = pack / "packages"
    package_dir.mkdir(parents=True)
    package = package_dir / "threat.json"
    package.write_text('{"schema_version":"packetsafari-threat-intel-v1"}', encoding="utf-8")
    manifest = {
        "schema_version": "packetsafari-security-content-v1",
        "channel": "stable",
        "sequence": 3,
        "version": "2026.08.13",
        "packages": [
            {
                "id": "threat",
                "type": "threat_intel_snapshot",
                "version": "1",
                "path": "packages/threat.json",
                "size": package.stat().st_size,
                "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                "provider": "test",
                "license": "MIT",
            }
        ],
    }
    manifest_path = pack / "content-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private_key)], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)], check=True, capture_output=True)
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(pack / "content-manifest.json.sig"), str(manifest_path)], check=True)
    archive = tmp_path / "security-content.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(pack, arcname="security-content-pack")
    return archive, public_key


def test_security_content_pack_host_verification(tmp_path: Path) -> None:
    archive, public_key = _signed_pack(tmp_path)
    extracted = operations._safe_extract_content_pack(archive, tmp_path / "extracted")
    result = operations._verify_content_pack(extracted, public_key)
    assert result == {
        "ok": True,
        "channel": "stable",
        "sequence": 3,
        "version": "2026.08.13",
        "packages": 1,
        "bytes": (extracted / "packages" / "threat.json").stat().st_size,
        "manifestSha256": hashlib.sha256((extracted / "content-manifest.json").read_bytes()).hexdigest(),
    }


def test_security_content_verification_rejects_tampering(tmp_path: Path) -> None:
    archive, public_key = _signed_pack(tmp_path)
    extracted = operations._safe_extract_content_pack(archive, tmp_path / "extracted")
    (extracted / "packages" / "threat.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="size mismatch|digest mismatch"):
        operations._verify_content_pack(extracted, public_key)


def test_security_content_materialization_is_size_bounded(tmp_path: Path) -> None:
    source = tmp_path / "content.tar.gz"
    source.write_bytes(b"too large")
    with pytest.raises(RuntimeError, match="exceeds"):
        operations.materialize_source(
            source,
            tmp_path / "downloads",
            "security-content pack",
            default_name="content.tar.gz",
            max_bytes=1,
        )


def test_content_cli_supports_connected_and_air_gapped_actions() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["content", "check", "--pack", "https://example.test/content.tar.gz"]).action == "check"
    assert parser.parse_args(["content", "import", "--pack", "/media/content.tar.gz"]).action == "import"
    assert parser.parse_args(["content", "status"]).action == "status"
    assert parser.parse_args(["content", "rollback"]).action == "rollback"


@pytest.mark.parametrize(
    ("payload", "expected_ok"),
    [
        (
            {
                "ok": True,
                "status": "disabled",
                "autoUpdateEnabled": False,
                "contentChannel": {"status": "inactive"},
            },
            True,
        ),
        (
            {
                "ok": False,
                "status": "healthy",
                "autoUpdateEnabled": True,
                "contentChannel": {"status": "error", "lastError": "bad signature"},
            },
            False,
        ),
    ],
)
def test_doctor_intelligence_probe_reports_air_gap_and_content_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    expected_ok: bool,
) -> None:
    layout = operations.runtime_layout(str(tmp_path), "/storage/onprem")
    layout.compose_dir.mkdir(parents=True)
    layout.compose_file.write_text("services:\n  backend:\n    image: test\n", encoding="utf-8")
    monkeypatch.setattr(operations, "_compose_base_command", lambda _layout: ["docker", "compose"])
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""),
    )
    result = operations._backend_intelligence_probe(layout)
    assert result["ok"] is expected_ok
    assert result["autoUpdateEnabled"] is payload["autoUpdateEnabled"]
