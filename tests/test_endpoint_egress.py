import json
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from packetsafari_onprem import operations


@pytest.fixture
def installed(tmp_path, monkeypatch):
    layout = operations.runtime_layout(str(tmp_path), str(tmp_path))
    shutil.copytree(Path(__file__).resolve().parents[1] / "templates" / "egress-config", layout.configuration_dir)
    monkeypatch.setattr(operations.socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(operations, "_restart_ironproxy_if_running", lambda _: False)
    return layout


def args(layout, action, **overrides):
    return SimpleNamespace(runtime_root=str(layout.runtime_root), container_runtime_root=str(layout.runtime_root),
        action=action, url="https://model.example/v1", approved_by="operator", notes="test", **overrides)


@pytest.mark.parametrize("purpose", ["ai", "identity"])
def test_approve_list_remove_preserves_other_policy(installed, purpose):
    before = json.loads(installed.production_egress_allowlist_path.read_text())["destinations"]
    result = operations.operate_intelligence_egress(args(installed, f"approve-{purpose}-host"))
    assert result["changed"] and not result["restarted"]
    assert '"model.example"' in installed.production_ironproxy_config_path.read_text()
    listed = operations.operate_intelligence_egress(args(installed, f"list-{purpose}-hosts"))
    assert any(entry["host"] == "model.example" for entry in listed["approvedHosts"])
    operations.operate_intelligence_egress(args(installed, f"remove-{purpose}-host"))
    assert '"model.example"' not in installed.production_ironproxy_config_path.read_text()
    assert json.loads(installed.production_egress_allowlist_path.read_text())["destinations"] == before


def test_ai_grants_preserve_scope_and_last_removal_revokes_proxy(installed):
    for team in ["a", "b"]:
        operations.operate_intelligence_egress(args(installed, "approve-ai-host", organization_id=team))
    listed = operations.operate_intelligence_egress(args(installed, "list-ai-hosts"))
    assert listed["approvedHosts"][0]["organization_ids"] == ["a", "b"]
    operations.operate_intelligence_egress(args(installed, "remove-ai-host", organization_id="a"))
    assert '"model.example"' in installed.production_ironproxy_config_path.read_text()
    operations.operate_intelligence_egress(args(installed, "remove-ai-host", organization_id="b"))
    assert '"model.example"' not in installed.production_ironproxy_config_path.read_text()


def test_team_approval_does_not_narrow_global_grant(installed):
    operations.operate_intelligence_egress(args(installed, "approve-ai-host"))
    operations.operate_intelligence_egress(args(installed, "approve-ai-host", organization_id="a"))
    listed = operations.operate_intelligence_egress(args(installed, "list-ai-hosts"))
    assert listed["approvedHosts"][0]["organization_ids"] == []
    with pytest.raises(RuntimeError, match="deployment-wide"):
        operations.operate_intelligence_egress(args(installed, "remove-ai-host", organization_id="a"))


def test_sync_failure_restores_files(installed, monkeypatch):
    before = {path: path.read_bytes() for path in installed.configuration_dir.rglob("*") if path.is_file()}
    def fail(*a, **kw):
        raise RuntimeError("sync failure")
    monkeypatch.setattr(operations, "_replace_ironproxy_domains", fail)
    with pytest.raises(RuntimeError, match="sync failure"):
        operations.operate_intelligence_egress(args(installed, "approve-ai-host"))
    for path, contents in before.items():
        assert path.read_bytes() == contents


def test_saas_rejects_private_dns_and_http(installed, monkeypatch):
    monkeypatch.setattr(operations, "_active_deployment_profile", lambda _: "saas")
    monkeypatch.setattr(operations.socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.1.1", 443))])
    with pytest.raises(RuntimeError, match="forbidden"):
        operations.operate_intelligence_egress(args(installed, "approve-ai-host"))
    request = args(installed, "approve-ai-host")
    request.url = "http://model.example"
    with pytest.raises(RuntimeError, match="HTTPS"):
        operations.operate_intelligence_egress(request)


@pytest.mark.parametrize("url", ['https://bad"host.example', 'https://169.254.169.254', 'https://127.0.0.1', 'https://model.example:0'])
def test_unsafe_hosts_cannot_enter_proxy_config(installed, url):
    request = args(installed, "approve-ai-host")
    request.url = url
    with pytest.raises(RuntimeError):
        operations.operate_intelligence_egress(request)


def test_other_purpose_keeps_hostname_allowed(installed):
    operations.operate_intelligence_egress(args(installed, "approve-ai-host"))
    operations.operate_intelligence_egress(args(installed, "approve-identity-host"))
    operations.operate_intelligence_egress(args(installed, "remove-ai-host"))
    assert '"model.example"' in installed.production_ironproxy_config_path.read_text()


@pytest.mark.parametrize("purpose", ["ai", "identity"])
def test_cli_dispatches_installed_commands(purpose, monkeypatch):
    from packetsafari_onprem import cli
    calls = []
    monkeypatch.setattr(cli, "operate_intelligence_egress", lambda request: calls.append(request) or {"ok": True})
    assert cli.main(["egress", f"approve-{purpose}-host", "--url", "https://model.example"]) == 0
    assert calls[0].action == f"approve-{purpose}-host"
    assert calls[0].url == "https://model.example"
