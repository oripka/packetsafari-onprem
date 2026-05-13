from __future__ import annotations

from types import SimpleNamespace

from packetsafari_onprem import operations


def test_saas_update_defaults_to_private_s3_channel(tmp_path):
    layout = SimpleNamespace(
        deployment_state_path=tmp_path / "deployment-state.json",
        release_manifest_path=tmp_path / "release-manifest.json",
        secrets_dir=tmp_path / "secrets",
    )
    layout.secrets_dir.mkdir()
    (layout.secrets_dir / "saas-operator-token").write_text("token\n", encoding="utf-8")

    source = operations._update_manifest_source(SimpleNamespace(profile=None, channel="stable", platform="linux-arm64"), layout)

    assert source == (
        "s3://packetsafari-release-channels-166826692770/"
        "channels/saas/stable/linux-arm64/release-manifest.json"
    )


def test_onprem_update_keeps_public_https_channel(tmp_path):
    layout = SimpleNamespace(
        deployment_state_path=tmp_path / "deployment-state.json",
        release_manifest_path=tmp_path / "release-manifest.json",
        secrets_dir=tmp_path / "secrets",
    )
    layout.secrets_dir.mkdir()

    source = operations._update_manifest_source(SimpleNamespace(profile="onprem", channel="stable", platform="linux-arm64"), layout)

    assert source == "https://releases.packetsafari.com/channels/onprem/stable/linux-arm64/release-manifest.json"


def test_materialize_source_copies_s3_with_aws_cli(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    monkeypatch.setattr(operations.shutil, "which", lambda name: "/usr/bin/aws" if name == "aws" else None)

    def fake_run(cmd, check):
        calls.append(list(cmd))
        destination = cmd[4]
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write('{"version":"test"}')

    monkeypatch.setattr(operations.subprocess, "run", fake_run)

    result = operations.materialize_source(
        "s3://bucket/channels/saas/stable/linux-arm64/release-manifest.json",
        tmp_path,
        "update manifest",
        default_name="release-manifest.json",
    )

    assert result.read_text(encoding="utf-8") == '{"version":"test"}'
    assert calls == [[
        "/usr/bin/aws",
        "s3",
        "cp",
        "s3://bucket/channels/saas/stable/linux-arm64/release-manifest.json",
        str(tmp_path / "release-manifest.json.download"),
        "--only-show-errors",
        "--region",
        "eu-central-1",
    ]]
