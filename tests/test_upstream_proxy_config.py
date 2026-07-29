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
