from __future__ import annotations

from packetsafari_onprem import operations


def test_sizing_plan_leaves_index_concurrency_runtime_sized(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))

    monkeypatch.setattr(
        operations,
        "_host_resource_snapshot",
        lambda _layout: {
            "vcpus": 32,
            "memoryBytes": 64 * operations.GIB,
            "disk": {"path": str(tmp_path), "totalBytes": 1, "usedBytes": 0, "freeBytes": 1},
        },
    )

    plan = operations._build_sizing_plan(layout, "auto")
    env = plan["env"]

    assert env["CELERY_INDEX_CONCURRENCY"] == "auto"
    assert env["PACKETSAFARI_CELERY_INDEX_CONCURRENCY_MAX"] == "24"
    assert env["PACKETSAFARI_CELERY_INDEX_CPU_FRACTION"] == "0.85"
    assert env["PACKETSAFARI_CELERY_INDEX_MEMORY_PER_TASK_MIB"] == "1536"
    assert env["PACKETSAFARI_CELERY_INDEX_MEMORY_RESERVE_MIB"] == "4096"
    assert env["PACKETSAFARI_MAINTENANCE_STORAGE_CLEANUP_RESCHEDULE_ENABLED"] == "true"
    assert "MAINTENANCE_STORAGE_CLEANUP_RESCHEDULE_ENABLED" not in env
    assert plan["services"]["worker"]["cpus"] == 24.0
    assert plan["services"]["sharkd"]["cpus"] == 17.5
    for service in plan["services"].values():
        assert service["memoryLimited"] is False
        assert "memLimit" not in service
    assert plan["services"]["worker"]["memoryBytes"] >= 34 * operations.GIB


def test_sizing_plan_keeps_index_task_memory_within_worker_limit(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))

    monkeypatch.setattr(
        operations,
        "_host_resource_snapshot",
        lambda _layout: {
            "vcpus": 1,
            "memoryBytes": 8 * operations.GIB,
            "disk": {"path": str(tmp_path), "totalBytes": 1, "usedBytes": 0, "freeBytes": 1},
        },
    )

    plan = operations._build_sizing_plan(layout, "auto")
    env = plan["env"]
    worker_mib = int(plan["services"]["worker"]["memoryBytes"] / operations.MIB)
    reserve_mib = int(env["PACKETSAFARI_CELERY_INDEX_MEMORY_RESERVE_MIB"])
    per_task_mib = int(env["PACKETSAFARI_CELERY_INDEX_MEMORY_PER_TASK_MIB"])

    assert plan["effectiveProfile"] == "small"
    assert per_task_mib <= worker_mib - reserve_mib
    assert per_task_mib < 3072


def test_host_requirements_report_flags_hosts_below_supported_floor(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))

    monkeypatch.setattr(
        operations,
        "_host_resource_snapshot",
        lambda _layout: {
            "vcpus": 1,
            "memoryBytes": 8 * operations.GIB,
            "disk": {"path": str(tmp_path), "totalBytes": 1, "usedBytes": 0, "freeBytes": 1},
        },
    )

    report = operations.host_requirements_report(layout)

    assert report["ok"] is False
    assert report["minimum"]["vcpus"] == 2
    assert report["minimum"]["memoryBytes"] == 16 * operations.GIB
    assert len(report["warnings"]) == 2


def test_host_requirements_report_accepts_supported_floor_with_recommendation(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))

    monkeypatch.setattr(
        operations,
        "_host_resource_snapshot",
        lambda _layout: {
            "vcpus": 2,
            "memoryBytes": 16 * operations.GIB,
            "disk": {"path": str(tmp_path), "totalBytes": 1, "usedBytes": 0, "freeBytes": 1},
        },
    )

    report = operations.host_requirements_report(layout)

    assert report["ok"] is True
    assert report["recommendedSmall"]["vcpus"] == 4
    assert report["warnings"] == [
        "host meets the supported floor but is below the recommended small-production baseline of 4 vCPU and 16.0 GiB RAM"
    ]


def test_sizing_state_status_flags_host_resize(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    operations._write_json(
        layout.sizing_state_path,
        {
            "host": {
                "vcpus": 1,
                "memoryBytes": 8 * operations.GIB,
                "disk": {"path": str(tmp_path), "totalBytes": 1, "usedBytes": 0, "freeBytes": 1},
            },
        },
    )
    monkeypatch.setattr(
        operations,
        "_host_resource_snapshot",
        lambda _layout: {
            "vcpus": 2,
            "memoryBytes": 16 * operations.GIB,
            "disk": {"path": str(tmp_path), "totalBytes": 1, "usedBytes": 0, "freeBytes": 1},
        },
    )

    report = operations.sizing_state_status(layout)

    assert report["ok"] is False
    assert report["stale"] is True
    assert report["warnings"] == [
        "host vCPU count changed from 1 to 2",
        "host RAM changed from 8.0 GiB to 16.0 GiB",
    ]


def test_sizing_state_status_accepts_matching_saved_host(monkeypatch, tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    operations._write_json(
        layout.sizing_state_path,
        {
            "host": {
                "vcpus": 2,
                "memoryBytes": 16 * operations.GIB,
                "disk": {"path": str(tmp_path), "totalBytes": 1, "usedBytes": 0, "freeBytes": 1},
            },
        },
    )
    monkeypatch.setattr(
        operations,
        "_host_resource_snapshot",
        lambda _layout: {
            "vcpus": 2,
            "memoryBytes": 16 * operations.GIB - 128 * operations.MIB,
            "disk": {"path": str(tmp_path), "totalBytes": 1, "usedBytes": 0, "freeBytes": 1},
        },
    )

    report = operations.sizing_state_status(layout)

    assert report["ok"] is True
    assert report["stale"] is False
    assert report["warnings"] == []


def test_sizing_compose_resolves_worker_concurrency_at_container_start(tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.compose_file.parent.mkdir(parents=True, exist_ok=True)
    layout.compose_file.write_text("services:\n  frontend:\n    image: frontend:test\n", encoding="utf-8")
    plan = {
        "services": {
            name: {"cpus": 1, "memoryLimited": False}
            for name in ("frontend", "backend", "worker", "postgres", "redis", "sharkd", "audit-forwarder")
        },
        "env": {},
    }

    rendered = operations._render_sizing_compose(layout, plan)

    assert "python3 /app/scripts/resolve_worker_concurrency.py index" in rendered
    assert 'CELERY_INDEX_CONCURRENCY:-auto' in rendered
    assert "  frontend:" in rendered
    assert "mem_limit:" not in rendered
    assert '--save "3600 1 300 100 60 10000"' in rendered
    assert "--save 20 1" not in rendered


def test_sizing_compose_omits_frontend_when_base_compose_has_no_frontend(tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.compose_file.parent.mkdir(parents=True, exist_ok=True)
    layout.compose_file.write_text("services:\n  backend:\n    image: backend:test\n", encoding="utf-8")
    plan = {
        "services": {
            name: {"cpus": 1, "memoryLimited": False}
            for name in ("frontend", "backend", "worker", "postgres", "redis", "sharkd", "audit-forwarder")
        },
        "env": {},
    }

    rendered = operations._render_sizing_compose(layout, plan)

    assert "  frontend:" not in rendered
    assert "  backend:" in rendered
    assert "mem_limit:" not in rendered
