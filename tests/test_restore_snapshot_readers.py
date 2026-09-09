import json
import signal
from types import SimpleNamespace

import pytest

from packetsafari_onprem import operations


def test_data_restore_stops_current_capture_readers_before_replacing_compose(monkeypatch, tmp_path):
    events = []
    layout = object()
    monkeypatch.setattr(operations, "validate_data_snapshot", lambda *a: events.append("validate"))
    def stop(actual, *, services, timeout):
        assert actual is layout
        assert "worker" in services and "sharkd" in services and "agent-cli-runner" in services
        assert timeout == 120
        events.append("stop")
    monkeypatch.setattr(operations, "docker_compose_stop", stop)
    for name, event in [
        ("_restore_metadata_snapshot", "metadata"), ("render_logging_config", "logging"),
        ("restore_postgres", "postgres"), ("restore_storage", "storage"), ("docker_compose_up", "up"),
    ]:
        monkeypatch.setattr(operations, name, lambda *a, event=event, **kw: events.append(event))
    operations.restore_snapshot(layout, tmp_path, restore_data=True)
    assert events == ["validate", "stop", "metadata", "logging", "postgres", "storage", "up"]


def _backup(path, *, completed=True):
    path.mkdir(parents=True, exist_ok=True)
    (path / "postgres.dump").write_bytes(b"PGDMP-test")
    (path / "storage.tar").write_bytes(b"tar-test")
    record = {"postgresBackup": "postgres.dump", "storageBackup": "storage.tar"}
    if completed:
        record["completedAt"] = "2026-09-09T00:00:00Z"
    record["dataArtifacts"] = {
        name: {"sizeBytes": (path / name).stat().st_size, "sha256": operations._sha256(path / name)}
        for name in ("postgres.dump", "storage.tar")
    }
    (path / "snapshot.json").write_text(json.dumps(record))


@pytest.mark.parametrize("damage", ["incomplete", "missing", "truncated", "changed", "malformed"])
def test_invalid_backup_is_rejected_before_any_restore_mutation(tmp_path, monkeypatch, damage):
    _backup(tmp_path, completed=damage != "incomplete")
    if damage == "missing":
        (tmp_path / "storage.tar").unlink()
    if damage == "truncated":
        (tmp_path / "storage.tar").write_bytes(b"tar")
    if damage == "changed":
        (tmp_path / "storage.tar").write_bytes(b"bad-test")
    if damage == "malformed":
        (tmp_path / "snapshot.json").write_text('{')
    events = []
    for name in ("docker_compose_stop", "_restore_metadata_snapshot", "restore_postgres", "restore_storage", "docker_compose_up"):
        monkeypatch.setattr(operations, name, lambda *a, **kw: events.append("mutation"))
    with pytest.raises(RuntimeError, match="refused"):
        operations.restore_snapshot(object(), tmp_path, restore_data=True)
    assert events == []


def test_complete_backup_checksums_are_accepted(tmp_path):
    _backup(tmp_path)
    operations.validate_data_snapshot(object(), tmp_path)


def test_backup_completion_records_integrity_only_after_both_archives(tmp_path, monkeypatch):
    def postgres(_layout, path):
        (path / "postgres.dump").write_bytes(b"database")
    def storage(_layout, path):
        assert not (path / "snapshot.json").exists()
        (path / "storage.tar").write_bytes(b"storage")
    monkeypatch.setattr(operations, "backup_postgres", postgres)
    monkeypatch.setattr(operations, "backup_storage", storage)
    operations.complete_full_backup(object(), tmp_path)
    record = json.loads((tmp_path / "snapshot.json").read_text())
    assert record["completedAt"]
    assert set(record["dataArtifacts"]) == {"postgres.dump", "storage.tar"}
    operations.validate_data_snapshot(object(), tmp_path)


def test_legacy_completed_backup_validates_both_formats_before_mutation(tmp_path, monkeypatch):
    _backup(tmp_path)
    path = tmp_path / "snapshot.json"
    record = json.loads(path.read_text())
    record.pop("dataArtifacts")
    path.write_text(json.dumps(record))
    events = []
    monkeypatch.setattr(operations.subprocess, "run", lambda command, **kw: events.append(command))
    monkeypatch.setattr(operations, "docker_compose_run", lambda layout, service, args, **kw: events.append((service, args, kw)))
    operations.validate_data_snapshot(object(), tmp_path)
    assert events[0] == ["tar", "-tf", str(tmp_path / "storage.tar")]
    assert events[1] == ("postgres", ["pg_restore", "--file=/dev/null", "/backup/postgres.dump"],
                         {"extra_volumes": [f"{tmp_path}:/backup:ro"]})


def test_stop_missing_optional_service_does_not_stop_the_entire_stack(monkeypatch):
    monkeypatch.setattr(operations, "_present_compose_services", lambda *_: [])
    monkeypatch.setattr(operations.subprocess, "run", lambda *a, **kw: pytest.fail("must not stop all services"))
    operations.docker_compose_stop(object(), services=["agent-cli-runner"])


def test_external_recovery_does_not_restart_writers(tmp_path, monkeypatch):
    events = []
    for name in ("docker_compose_stop", "_restore_metadata_snapshot", "render_logging_config", "docker_compose_up"):
        monkeypatch.setattr(operations, name, lambda *a, name=name, **kw: events.append(name))
    operations.restore_snapshot(object(), tmp_path, restore_data=False, restart_services=False)
    assert events == ["docker_compose_stop", "_restore_metadata_snapshot", "render_logging_config"]


def test_onprem_rollback_refuses_latest_incomplete_backup(tmp_path, monkeypatch):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    operations.ensure_runtime_dirs(layout)
    _backup(layout.backup_dir / "20260908-100000")
    _backup(layout.backup_dir / "20260909-100000", completed=False)
    monkeypatch.setattr(operations, "docker_compose_stop", lambda *a, **kw: pytest.fail("must validate first"))
    with pytest.raises(RuntimeError, match="Incomplete"):
        operations.rollback_release(SimpleNamespace(runtime_root=str(tmp_path), container_runtime_root=str(tmp_path), profile="onprem"))


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_upgrade_signals_enter_recovery_and_restore_previous_handlers(sig):
    previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    with operations.upgrade_cancellation_guard():
        with pytest.raises(KeyboardInterrupt, match="operator cancellation"):
            signal.getsignal(sig)(sig, None)
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
    assert {s: signal.getsignal(s) for s in previous} == previous


@pytest.mark.parametrize("still_running", [False, True])
def test_interrupted_oneoff_is_stopped_before_recovery(monkeypatch, still_running):
    commands = []
    monkeypatch.setattr(operations, "_compose_base_command", lambda _layout: ["docker", "compose"])
    def run(command, **kwargs):
        commands.append(command)
        if "run" in command:
            raise KeyboardInterrupt()
        return SimpleNamespace(stdout="container-id" if still_running else "")
    monkeypatch.setattr(operations.subprocess, "run", run)
    with pytest.raises(operations.UpgradeProcessNotStopped if still_running else KeyboardInterrupt):
        operations.docker_compose_run(object(), "backend", ["migrate"])
    name = commands[0][commands[0].index("--name") + 1]
    assert commands[1] == ["docker", "rm", "-f", name]
    assert f"name=^/{name}$" in commands[2]
