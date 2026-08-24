from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from packetsafari_onprem import operations


@pytest.fixture
def signed_manifest(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for detached release manifest signatures")

    private_key = tmp_path / "release-private.pem"
    public_key = tmp_path / "release-public.pem"
    manifest = tmp_path / "release-manifest.json"
    signature = tmp_path / "release-manifest.json.sig"
    subprocess.run(
        [openssl, "genpkey", "-quiet", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private_key)],
        check=True,
    )
    subprocess.run(
        [openssl, "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)],
        check=True,
    )
    manifest.write_text('{"version":"10.0.1","tooling":{"archiveUrl":"https://example.invalid/tooling.tgz"}}\n')
    subprocess.run(
        [openssl, "dgst", "-sha256", "-sign", str(private_key), "-out", str(signature), str(manifest)],
        check=True,
    )

    layout = operations.runtime_layout(str(tmp_path / "runtime"), str(tmp_path / "runtime"))
    operations.ensure_runtime_dirs(layout)
    shutil.copy2(public_key, layout.release_public_key_path)
    args = SimpleNamespace(manifest_signature="", download_header=[])
    return layout, manifest, signature, args


def test_connected_manifest_is_verified_before_copy(signed_manifest):
    layout, manifest, _, args = signed_manifest

    target = operations.prepare_connected_manifest(layout, str(manifest), args)

    assert target == layout.target_release_manifest_path
    assert target.read_bytes() == manifest.read_bytes()


def test_connected_manifest_rejects_tampering(signed_manifest):
    layout, manifest, _, args = signed_manifest
    manifest.write_text('{"version":"attacker-controlled"}\n')

    with pytest.raises(RuntimeError, match="signature verification failed"):
        operations.prepare_connected_manifest(layout, str(manifest), args)

    assert not layout.target_release_manifest_path.exists()


def test_connected_manifest_rejects_profile_mismatch_before_copy(tmp_path, monkeypatch):
    layout = operations.runtime_layout(str(tmp_path / "runtime"), str(tmp_path / "runtime"))
    operations.ensure_runtime_dirs(layout)
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text('{"version":"10.0.1","targetProfile":"saas"}\n', encoding="utf-8")
    verified: list[str] = []

    def fake_verify(layout, manifest_source, args=None, destination=None):
        verified.append(str(manifest_source))
        return manifest

    monkeypatch.setattr(operations, "materialize_verified_release_manifest", fake_verify)

    with pytest.raises(RuntimeError, match="targets profile 'saas'"):
        operations.prepare_connected_manifest(
            layout,
            str(manifest),
            SimpleNamespace(profile="onprem"),
        )

    assert verified == [str(manifest)]
    assert not layout.target_release_manifest_path.exists()


def test_signed_url_with_query_requires_explicit_signature_source():
    args = SimpleNamespace(manifest_signature="")
    with pytest.raises(RuntimeError, match="--manifest-signature"):
        operations._manifest_signature_source(
            "https://downloads.example/release-manifest.json?token=manifest-only",
            args,
        )

    args.manifest_signature = "https://downloads.example/release-manifest.json.sig?token=signature"
    assert operations._manifest_signature_source("https://ignored.example/manifest.json?token=x", args) == args.manifest_signature
