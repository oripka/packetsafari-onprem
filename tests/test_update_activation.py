import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packetsafari_onprem import operations as ops, rolling_update


def test_update_resumes_activation_without_repeating_maintenance(tmp_path, monkeypatch):
    layout = ops.runtime_layout(str(tmp_path / 'runtime'), str(tmp_path / 'runtime'))
    layout.state_dir.mkdir(parents=True)
    source = tmp_path / 'release.json'
    source.write_text(json.dumps({'version': '10.0.0-test', 'runtimeContract': {'protocolVersion': 1}}))
    Path(str(source) + '.sig').write_bytes(b'signed-fixture')
    policy = tmp_path / 'ingress.json'
    policy.write_text(json.dumps({'mode': 'cloudfront-https', 'trustedCidrs': ['192.0.2.10/32'],
                                  'viewerHttpsOnly': True}))
    args = SimpleNamespace(runtime_root=str(tmp_path / 'runtime'), container_runtime_root=str(tmp_path / 'runtime'),
                           activate_deployment_proxy=True, maintenance=True, ingress_policy=policy,
                           manifest_url=None, manifest_signature=None, profile='saas', force=False,
                           human_output=False, backup_mode='skip', allow_unbacked_upgrade=True)
    monkeypatch.setattr(ops, 'warn_if_host_below_requirements', lambda _: {'warnings': []})
    monkeypatch.setattr(ops, 'warn_if_sizing_state_stale', lambda _: {'stale': False})
    monkeypatch.setattr(ops, '_download_update_manifest', lambda a, _: Path(a.manifest_url) if a.manifest_url else source)
    monkeypatch.setattr(ops, '_update_check_payload', lambda a, l, p: {
        'available': ops._release_version(l.release_manifest_path) != '10.0.0-test',
        'app': {'currentVersion': ops._release_version(l.release_manifest_path), 'targetVersion': '10.0.0-test'},
        'backupMode': 'skip', 'ops': {'currentVersion': ops.version()}, 'changedServices': ['backend'],
    })
    monkeypatch.setattr(ops, 'acknowledge_unbacked_upgrade', lambda *a, **kw: None)
    monkeypatch.setattr(ops, 'maybe_self_update_tooling', lambda *a, **kw: None)
    monkeypatch.setattr(ops, 'maybe_offer_docker_image_prune', lambda *a: {'status': 'not-needed'})
    monkeypatch.setattr(rolling_update, 'enabled', lambda _: False)
    maintenance_calls = []

    def maintenance(_):
        maintenance_calls.append(True)
        layout.release_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        layout.release_manifest_path.write_bytes(source.read_bytes())
        return {'version': '10.0.0-test', 'backupMode': 'skip'}

    monkeypatch.setattr(ops, 'upgrade_release', maintenance)
    activation_calls = []

    def activate(a):
        activation_calls.append((a.manifest, a.manifest_signature, a.ingress_policy))
        if len(activation_calls) == 1:
            raise RuntimeError('simulated interruption after app install')
        return {'status': 'enabled'}

    monkeypatch.setattr(rolling_update, 'manage_host', activate)
    with pytest.raises(RuntimeError, match='simulated interruption'):
        ops.apply_update(args)
    journal = layout.state_dir / 'rolling' / 'activation.json'
    assert json.loads(journal.read_text())['phase'] == 'app-installed'
    assert len(maintenance_calls) == 1
    assert Path(activation_calls[0][0]).read_bytes() == source.read_bytes()
    assert ops.apply_update(args)['activation']['status'] == 'enabled'
    assert len(maintenance_calls) == 1
    assert not journal.exists()
