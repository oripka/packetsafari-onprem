from __future__ import annotations

from types import SimpleNamespace

import pytest

from packetsafari_onprem import operations
from packetsafari_onprem.envfile import parse_env_file


def _args(tmp_path, **overrides):
    values = {
        "runtime_root": str(tmp_path),
        "container_runtime_root": str(tmp_path),
        "proxy_url": "",
        "http_proxy": "",
        "https_proxy": "",
        "no_proxy": None,
        "clear": False,
        "restart": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_configure_upstream_proxy_sets_both_proxy_env_values(tmp_path):
    result = operations.configure_upstream_proxy(
        _args(
            tmp_path,
            proxy_url="http://zscaler.example:8080",
            no_proxy="localhost,127.0.0.1,::1",
        )
    )

    env_path = tmp_path / "env" / "ironproxy.env"
    values = parse_env_file(env_path)

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["changedKeys"] == ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]
    assert values["HTTP_PROXY"] == "http://zscaler.example:8080"
    assert values["HTTPS_PROXY"] == "http://zscaler.example:8080"
    assert values["NO_PROXY"] == "localhost,127.0.0.1,::1"


def test_configure_upstream_proxy_preserves_existing_ironproxy_secrets(tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.ironproxy_env_path.parent.mkdir(parents=True)
    layout.ironproxy_env_path.write_text(
        'OPENAI_API_KEY="secret"\nOPENROUTER_API_KEY="router-secret"\n',
        encoding="utf-8",
    )

    operations.configure_upstream_proxy(_args(tmp_path, https_proxy="http://proxy.example:8080"))

    values = parse_env_file(layout.ironproxy_env_path)
    assert values["OPENAI_API_KEY"] == "secret"
    assert values["OPENROUTER_API_KEY"] == "router-secret"
    assert values["HTTPS_PROXY"] == "http://proxy.example:8080"


def test_configure_upstream_proxy_clear_removes_proxy_keys_only(tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    layout.ironproxy_env_path.parent.mkdir(parents=True)
    layout.ironproxy_env_path.write_text(
        '\n'.join(
            [
                'OPENAI_API_KEY="secret"',
                'OPENROUTER_API_KEY="router-secret"',
                'HTTP_PROXY="http://proxy.example:8080"',
                'HTTPS_PROXY="http://proxy.example:8080"',
                'NO_PROXY="localhost"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = operations.configure_upstream_proxy(_args(tmp_path, clear=True))

    values = parse_env_file(layout.ironproxy_env_path)
    assert result["cleared"] is True
    assert values == {
        "OPENAI_API_KEY": "secret",
        "OPENROUTER_API_KEY": "router-secret",
    }


def test_configure_upstream_proxy_rejects_invalid_urls(tmp_path):
    with pytest.raises(RuntimeError, match="http:// or https://"):
        operations.configure_upstream_proxy(_args(tmp_path, proxy_url="zscaler.example:8080"))


def test_configure_upstream_proxy_can_restart_egress_ironproxy(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(operations, "diagnostics_restart", lambda args: calls.append(args.service))

    result = operations.configure_upstream_proxy(
        _args(tmp_path, proxy_url="http://zscaler.example:8080", restart=True)
    )

    assert result["restarted"] is True
    assert calls == ["egress-ironproxy"]


def test_egress_mode_is_explicit_and_reversible(tmp_path, monkeypatch):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    operations._write_json(
        layout.production_egress_allowlist_path,
        {
            "destinations": [{"host": "api.openai.com", "port": 443}],
            "monitor_mode": False,
            "version": 1,
        },
    )
    layout.production_ironproxy_config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.production_ironproxy_config_path.write_text(
        "transforms:\n"
        "  - name: allowlist\n"
        "    config:\n"
        "      domains:\n"
        '        - "api.openai.com"\n'
        "log:\n"
        '  level: "info"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(operations, "_restart_ironproxy_if_running", lambda layout: True)

    unrestricted = operations.operate_intelligence_egress(
        _args(tmp_path, action="mode", egress_mode="unrestricted")
    )

    assert unrestricted["previousMode"] == "allowlist"
    assert unrestricted["egressMode"] == "unrestricted"
    assert unrestricted["restarted"] is True
    assert operations._read_json(layout.production_egress_allowlist_path)["monitor_mode"] is True
    assert "      warn: true\n      domains:\n" in layout.production_ironproxy_config_path.read_text()

    restored = operations.operate_intelligence_egress(
        _args(tmp_path, action="mode", egress_mode="allowlist")
    )

    assert restored["previousMode"] == "unrestricted"
    assert restored["egressMode"] == "allowlist"
    assert operations._read_json(layout.production_egress_allowlist_path)["monitor_mode"] is False
    assert "warn:" not in layout.production_ironproxy_config_path.read_text()


@pytest.mark.parametrize(
    ("initial_mode", "requested_mode", "initial_warn"),
    [
        ("allowlist", "unrestricted", False),
        ("unrestricted", "allowlist", True),
    ],
)
def test_egress_mode_restores_persisted_and_live_config_after_restart_failure(
    tmp_path,
    monkeypatch,
    initial_mode,
    requested_mode,
    initial_warn,
):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    operations._write_json(
        layout.production_egress_allowlist_path,
        {
            "destinations": [{"host": "api.openai.com", "port": 443}],
            "monitor_mode": initial_mode == "unrestricted",
            "version": 1,
        },
    )
    layout.production_ironproxy_config_path.parent.mkdir(parents=True, exist_ok=True)
    warn_line = "      warn: true\n" if initial_warn else ""
    layout.production_ironproxy_config_path.write_text(
        "transforms:\n"
        "  - name: allowlist\n"
        "    config:\n"
        f"{warn_line}"
        "      domains:\n"
        '        - "api.openai.com"\n',
        encoding="utf-8",
    )
    previous_allowlist = layout.production_egress_allowlist_path.read_bytes()
    previous_proxy_config = layout.production_ironproxy_config_path.read_bytes()
    restart_calls = 0

    def fail_then_restore(_layout):
        nonlocal restart_calls
        restart_calls += 1
        if restart_calls == 1:
            raise RuntimeError("injected restart failure")
        return True

    monkeypatch.setattr(operations, "_restart_ironproxy_if_running", fail_then_restore)

    with pytest.raises(RuntimeError, match=f"previous {initial_mode} mode was restored"):
        operations.operate_intelligence_egress(
            _args(tmp_path, action="mode", egress_mode=requested_mode)
        )

    assert restart_calls == 2
    assert layout.production_egress_allowlist_path.read_bytes() == previous_allowlist
    assert layout.production_ironproxy_config_path.read_bytes() == previous_proxy_config


@pytest.mark.parametrize(
    ("initial_mode", "requested_mode", "initial_warn"),
    [
        ("allowlist", "unrestricted", False),
        ("unrestricted", "allowlist", True),
    ],
)
def test_egress_mode_restores_files_after_sync_failure(
    tmp_path,
    monkeypatch,
    initial_mode,
    requested_mode,
    initial_warn,
):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    operations._write_json(
        layout.production_egress_allowlist_path,
        {
            "destinations": [{"host": "api.openai.com", "port": 443}],
            "monitor_mode": initial_mode == "unrestricted",
            "version": 1,
        },
    )
    layout.production_ironproxy_config_path.parent.mkdir(parents=True, exist_ok=True)
    warn_line = "      warn: true\n" if initial_warn else ""
    layout.production_ironproxy_config_path.write_text(
        "transforms:\n"
        "  - name: allowlist\n"
        "    config:\n"
        f"{warn_line}"
        "      domains:\n"
        '        - "api.openai.com"\n',
        encoding="utf-8",
    )
    previous_allowlist = layout.production_egress_allowlist_path.read_bytes()
    previous_proxy_config = layout.production_ironproxy_config_path.read_bytes()
    sync_config = operations._sync_intelligence_egress_config

    def fail_after_sync(_layout):
        sync_config(_layout)
        raise RuntimeError("injected sync failure")

    monkeypatch.setattr(operations, "_sync_intelligence_egress_config", fail_after_sync)
    monkeypatch.setattr(
        operations,
        "_restart_ironproxy_if_running",
        lambda _layout: pytest.fail("restart must not run after sync failure"),
    )

    with pytest.raises(RuntimeError, match=f"previous {initial_mode} mode was restored"):
        operations.operate_intelligence_egress(
            _args(tmp_path, action="mode", egress_mode=requested_mode)
        )

    assert layout.production_egress_allowlist_path.read_bytes() == previous_allowlist
    assert layout.production_ironproxy_config_path.read_bytes() == previous_proxy_config


def test_egress_mode_reports_incomplete_live_rollback(tmp_path, monkeypatch):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    operations._write_json(
        layout.production_egress_allowlist_path,
        {"destinations": [], "monitor_mode": False, "version": 1},
    )
    layout.production_ironproxy_config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.production_ironproxy_config_path.write_text(
        "transforms:\n"
        "  - name: allowlist\n"
        "    config:\n"
        "      domains:\n",
        encoding="utf-8",
    )
    restart_calls = 0

    def fail_without_recovery(_layout):
        nonlocal restart_calls
        restart_calls += 1
        if restart_calls == 1:
            raise RuntimeError("injected restart failure")
        return False

    monkeypatch.setattr(operations, "_restart_ironproxy_if_running", fail_without_recovery)

    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        operations.operate_intelligence_egress(
            _args(tmp_path, action="mode", egress_mode="unrestricted")
        )

    assert operations._read_json(layout.production_egress_allowlist_path)["monitor_mode"] is False
    assert "warn:" not in layout.production_ironproxy_config_path.read_text()


def test_unrestricted_egress_mode_is_rejected_for_saas(tmp_path):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    operations._write_json(layout.deployment_state_path, {"deployment": {"profile": "saas"}})

    with pytest.raises(RuntimeError, match="only for customer-operated on-prem"):
        operations.operate_intelligence_egress(
            _args(tmp_path, action="mode", egress_mode="unrestricted")
        )


def test_render_compose_preserves_explicit_unrestricted_mode(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    tooling_root = tmp_path / "tooling"
    layout = operations.runtime_layout(str(runtime_root), str(runtime_root))
    operations._write_json(
        layout.production_egress_allowlist_path,
        {"destinations": [], "monitor_mode": True, "version": 1},
    )

    template_config = tooling_root / "templates" / "egress-config"
    operations._write_json(
        template_config / "egress-allowlist.production.yaml",
        {
            "destinations": [{"host": "api.openai.com", "port": 443}],
            "monitor_mode": False,
            "version": 1,
        },
    )
    proxy_path = template_config / "iron-proxy" / "proxy.production.generated.yaml"
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    proxy_path.write_text(
        "transforms:\n"
        "  - name: allowlist\n"
        "    config:\n"
        "      domains:\n"
        '        - "api.openai.com"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(operations, "_run_script", lambda *args, **kwargs: None)

    operations.render_compose(
        layout,
        layout.release_manifest_path,
        source_root=tooling_root,
        profile="onprem",
    )

    assert operations._read_json(layout.production_egress_allowlist_path)["monitor_mode"] is True
    assert "      warn: true\n      domains:\n" in layout.production_ironproxy_config_path.read_text()
