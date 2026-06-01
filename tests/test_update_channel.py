from __future__ import annotations

import hashlib
import json
import tarfile
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


def test_ensure_journald_retention_config_writes_bounded_policy(tmp_path, monkeypatch):
    config_path = tmp_path / "journald.conf.d" / "packetsafari.conf"
    calls: list[list[str]] = []

    monkeypatch.setattr(operations, "JOURNALD_RETENTION_CONFIG_PATH", config_path)
    monkeypatch.setattr(operations.shutil, "which", lambda name: "/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(operations.subprocess, "run", lambda command, check=False, **_: calls.append(list(command)))

    result = operations.ensure_journald_retention_config()

    assert result == {"path": str(config_path), "changed": True, "restarted": True}
    content = config_path.read_text(encoding="utf-8")
    assert "SystemMaxUse=2G" in content
    assert "MaxRetentionSec=7day" in content
    assert calls == [["systemctl", "restart", "systemd-journald"]]

    calls.clear()
    result = operations.ensure_journald_retention_config()

    assert result == {"path": str(config_path), "changed": False, "restarted": False}
    assert calls == []


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


def test_update_check_payload_reports_app_and_ops_layers(tmp_path):
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(
        """
{
  "version": "10.0.1",
  "channel": "stable",
  "tooling": {
    "version": "99.0.0",
    "minOpsVersion": "99.0.0",
    "archiveUrl": "s3://bucket/tooling/packetsafari-onprem-99.0.0.tar.gz",
    "sha256": "abc"
  }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))

    payload = operations._update_check_payload(
        SimpleNamespace(profile="saas", channel="stable", platform="linux-arm64", backup_mode=None),
        layout,
        manifest_path,
    )

    assert payload["app"]["available"] is True
    assert payload["app"]["targetVersion"] == "10.0.1"
    assert payload["ops"]["available"] is True
    assert payload["ops"]["targetVersion"] == "99.0.0"
    assert payload["tooling"] == payload["ops"]


def test_maybe_self_update_tooling_installs_archive_without_reexec(monkeypatch, tmp_path):
    source_root = tmp_path / "source" / "packetsafari-onprem"
    package_dir = source_root / "packetsafari_onprem"
    package_dir.mkdir(parents=True)
    (package_dir / "cli.py").write_text("print('new cli')\n", encoding="utf-8")
    (package_dir / "__init__.py").write_text("__version__ = '99.0.0'\n", encoding="utf-8")

    archive = tmp_path / "packetsafari-onprem-99.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source_root, arcname=source_root.name)

    manifest = {
        "tooling": {
            "version": "99.0.0",
            "minOpsVersion": "99.0.0",
            "archiveUrl": str(archive),
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
    }
    layout = operations.runtime_layout(str(tmp_path / "runtime"), str(tmp_path / "runtime"))
    monkeypatch.setenv("PACKETSAFARI_OPS_SELF_UPDATE_NO_REEXEC", "true")
    monkeypatch.setattr(operations, "install_global_wrapper", lambda layout: None)

    result = operations.maybe_self_update_tooling(
        SimpleNamespace(download_header=[], download_basic="", download_bearer_token="", allow_insecure_download=False),
        layout,
        manifest,
    )

    assert result["updated"] is True
    assert (layout.tooling_root / "packetsafari_onprem" / "cli.py").read_text(encoding="utf-8") == "print('new cli')\n"
    assert layout.wrapper_path.exists()


def test_services_with_changed_images_only_returns_changed_service_images():
    active = {
        "images": {
            "frontend": "repo/frontend:1",
            "backend": "repo/backend:1",
            "sharkd": "repo/sharkd:1",
            "egress-firewall": "repo/firewall:1",
            "egress-ironproxy": "repo/ironproxy:1",
            "egress-dns": "repo/dns:1",
        }
    }
    target = {
        "images": {
            "frontend": "repo/frontend:1",
            "backend": "repo/backend:2",
            "sharkd": "repo/sharkd:1",
            "egress-firewall": "repo/firewall:1",
            "egress-ironproxy": "repo/ironproxy:1",
            "egress-dns": "repo/dns:1",
        }
    }

    assert operations._services_with_changed_images(active, target) == ["backend", "worker"]


def test_services_with_changed_images_keeps_unchanged_sharkd_and_firewall_out():
    active = {
        "images": {
            "frontend": "repo/frontend:1",
            "backend": "repo/backend:1",
            "worker": "repo/worker:1",
            "sharkd": "repo/sharkd:1",
            "egress-firewall": "repo/firewall:1",
            "egress-ironproxy": "repo/ironproxy:1",
            "egress-dns": "repo/dns:1",
        }
    }
    target = {
        "images": {
            "frontend": "repo/frontend:2",
            "backend": "repo/backend:1",
            "worker": "repo/worker:1",
            "sharkd": "repo/sharkd:1",
            "egress-firewall": "repo/firewall:1",
            "egress-ironproxy": "repo/ironproxy:2",
            "egress-dns": "repo/dns:1",
        }
    }

    assert operations._services_with_changed_images(active, target) == ["frontend", "egress-ironproxy"]


def test_image_retention_blocks_prune_until_history_has_keep_set(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.state_dir.mkdir(parents=True)
    layout.compose_dir.mkdir(parents=True)
    layout.env_dir.mkdir(parents=True)
    layout.release_manifest_path.write_text(
        json.dumps({"version": "10.0.0-beta.3", "images": {"backend": "repo/backend:3"}}),
        encoding="utf-8",
    )
    layout.deployment_state_path.write_text(
        json.dumps(
            {
                "imageRetention": {
                    "history": [
                        {"version": "10.0.0-beta.3", "images": {"backend": {"id": "sha256:current"}}},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(operations.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(operations, "_docker_container_image_ids", lambda: {"sha256:current"})
    monkeypatch.setattr(operations, "_docker_image_id", lambda ref: "sha256:current" if ref == "repo/backend:3" else "")
    monkeypatch.setattr(
        operations,
        "_dangling_docker_images",
        lambda: [
            {"id": "sha256:old", "sizeBytes": 1_500_000_000, "size": "1.5GB", "createdSince": "2 weeks ago"},
        ],
    )

    health = operations.docker_image_retention_health(layout, keep_deployments=2)

    assert health["candidateCount"] == 1
    assert health["safeToPrune"] is False
    assert health["recordedDeployments"] == 1


def test_image_retention_prunes_only_unprotected_dangling_images(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.state_dir.mkdir(parents=True)
    layout.compose_dir.mkdir(parents=True)
    layout.env_dir.mkdir(parents=True)
    layout.release_manifest_path.write_text(
        json.dumps({"version": "10.0.0-beta.3", "images": {"backend": "repo/backend:3"}}),
        encoding="utf-8",
    )
    layout.deployment_state_path.write_text(
        json.dumps(
            {
                "imageRetention": {
                    "history": [
                        {"version": "10.0.0-beta.1", "images": {"backend": {"id": "sha256:old-protected"}}},
                        {"version": "10.0.0-beta.2", "images": {"backend": {"id": "sha256:previous"}}},
                        {"version": "10.0.0-beta.3", "images": {"backend": {"id": "sha256:current"}}},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    removed: list[list[str]] = []

    monkeypatch.setattr(operations.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(operations, "_docker_container_image_ids", lambda: {"sha256:current"})
    monkeypatch.setattr(operations, "_docker_image_id", lambda ref: "sha256:current" if ref == "repo/backend:3" else "")
    monkeypatch.setattr(
        operations,
        "_dangling_docker_images",
        lambda: [
            {"id": "sha256:previous", "sizeBytes": 500_000_000, "size": "500MB"},
            {"id": "sha256:old-unused", "sizeBytes": 700_000_000, "size": "700MB"},
        ],
    )

    def fake_run(command, *, check=False):
        removed.append(list(command))
        return SimpleNamespace(returncode=0, stdout="removed\n", stderr="")

    monkeypatch.setattr(operations, "_run_text", fake_run)

    result = operations.prune_old_docker_images(layout, keep_deployments=2)

    assert result["status"] == "ok"
    assert result["removedIds"] == ["sha256:old-unused"]
    assert removed == [["docker", "image", "rm", "sha256:old-unused"]]


def test_upgrade_pulls_target_images_before_stopping_changed_services(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    operations.ensure_runtime_dirs(layout)
    layout.release_manifest_path.write_text(
        '{"version":"10.0.0-beta.1","images":{"backend":"repo/backend:1","worker":"repo/worker:1"}}\n',
        encoding="utf-8",
    )
    layout.compose_file.write_text("services:\n  backend:\n  worker:\n", encoding="utf-8")
    layout.runtime_env_path.write_text("PACKETSAFARI_PUBLIC_BASE_URL=https://next.packetsafari.com\n", encoding="utf-8")
    layout.deployment_state_path.write_text('{"deployment":{"installedVersion":"10.0.0-beta.1"}}\n', encoding="utf-8")
    target_manifest = tmp_path / "target-manifest.json"
    target_manifest.write_text(
        '{"version":"10.0.0-beta.2","images":{"backend":"repo/backend:2","worker":"repo/worker:2"}}\n',
        encoding="utf-8",
    )
    calls: list[str] = []

    monkeypatch.setattr(operations, "sync_bundle", lambda layout: None)
    monkeypatch.setattr(operations, "prepare_connected_manifest", lambda layout, manifest_arg, args=None, destination=None: target_manifest)
    monkeypatch.setattr(operations, "maybe_self_update_tooling", lambda args, layout, manifest: None)
    monkeypatch.setattr(operations, "validate_tooling_requirement", lambda manifest: None)
    monkeypatch.setattr(operations, "validate_upgrade_path", lambda layout, manifest: None)
    monkeypatch.setattr(operations, "verify_saas_operator_authorization", lambda layout, args, manifest: None)
    monkeypatch.setattr(operations, "validate_required_env", lambda layout, manifest, profile: None)
    monkeypatch.setattr(operations, "ensure_ecr_credential_helper_ready", lambda layout: calls.append("ensure_ecr"))
    monkeypatch.setattr(operations, "render_logging_config", lambda layout: calls.append("render_logging"))
    monkeypatch.setattr(operations, "docker_compose_pull", lambda layout: calls.append("pull"))
    monkeypatch.setattr(operations, "docker_compose_stop", lambda layout, services=None, timeout=120: calls.append(f"stop:{','.join(services or [])}"))
    monkeypatch.setattr(operations, "run_target_migrations", lambda layout: calls.append("migrate"))
    monkeypatch.setattr(operations, "docker_compose_up", lambda layout, services=None, pull_policy=None: calls.append("up"))
    monkeypatch.setattr(operations, "wait_for_health", lambda timeout_seconds=180: calls.append("health"))
    monkeypatch.setattr(operations, "assert_doctor_ok", lambda args: calls.append("doctor"))

    def fake_render_compose(layout, manifest_path, source_root=None, profile="onprem"):
        calls.append("render")
        layout.compose_file.write_text("services:\n  backend:\n  worker:\n", encoding="utf-8")

    monkeypatch.setattr(operations, "render_compose", fake_render_compose)

    def fake_promote(layout, manifest, snapshot_dir, *, source, profile, backup_mode):
        calls.append("promote")
        return {"message": "ok"}

    monkeypatch.setattr(operations, "_promote_release", fake_promote)

    result = operations.upgrade_release(
        SimpleNamespace(
            runtime_root=str(tmp_path),
            container_runtime_root=str(tmp_path),
            profile="saas",
            backup_mode="skip",
            allow_unbacked_upgrade=True,
            bundle=None,
            manifest=str(target_manifest),
            skip_image_pull=False,
            skip_health_check=False,
            health_timeout=1,
            simulate_failure_phase="",
            max_backup_age_minutes=180,
        )
    )

    assert result == {"message": "ok"}
    assert calls.index("pull") < calls.index("stop:backend,worker")
    assert calls.index("render") < calls.index("pull")
    assert calls.index("stop:backend,worker") < calls.index("migrate")
