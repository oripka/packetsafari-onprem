from packetsafari_onprem import operations


def test_data_restore_stops_current_capture_readers_before_replacing_compose(monkeypatch, tmp_path):
    events = []
    layout = object()
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
    assert events == ["stop", "metadata", "logging", "postgres", "storage", "up"]
