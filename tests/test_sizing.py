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
    assert plan["services"]["worker"]["cpus"] == 24.0
    assert plan["services"]["sharkd"]["cpus"] == 17.5
    assert plan["services"]["worker"]["memoryBytes"] >= 34 * operations.GIB


def test_sizing_compose_resolves_worker_concurrency_at_container_start(tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.compose_file.parent.mkdir(parents=True, exist_ok=True)
    layout.compose_file.write_text("services:\n  frontend:\n    image: frontend:test\n", encoding="utf-8")
    plan = {
        "services": {
            name: {"cpus": 1, "memLimit": "1g"}
            for name in ("frontend", "backend", "worker", "postgres", "redis", "sharkd", "audit-forwarder")
        },
        "env": {},
    }

    rendered = operations._render_sizing_compose(layout, plan)

    assert "python3 /app/scripts/resolve_worker_concurrency.py index" in rendered
    assert 'CELERY_INDEX_CONCURRENCY:-auto' in rendered
    assert "  frontend:" in rendered


def test_sizing_compose_omits_frontend_when_base_compose_has_no_frontend(tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.compose_file.parent.mkdir(parents=True, exist_ok=True)
    layout.compose_file.write_text("services:\n  backend:\n    image: backend:test\n", encoding="utf-8")
    plan = {
        "services": {
            name: {"cpus": 1, "memLimit": "1g"}
            for name in ("frontend", "backend", "worker", "postgres", "redis", "sharkd", "audit-forwarder")
        },
        "env": {},
    }

    rendered = operations._render_sizing_compose(layout, plan)

    assert "  frontend:" not in rendered
    assert "  backend:" in rendered
