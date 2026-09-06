from __future__ import annotations

import json
from pathlib import Path

from scripts import render_compose


def _write_template(path):
    path.write_text(
        """services:
  frontend:
    image: "{{ frontend_image }}"
  backend:
    image: "{{ backend_image }}"
  worker:
    image: "{{ worker_image }}"
  postgres:
    image: "{{ postgres_image }}"
  redis:
    image: "{{ redis_image }}"
  sharkd:
    image: "{{ sharkd_image }}"
    {{ sharkd_network_exposure }}
  egress-dns:
    image: "{{ egress_dns_image }}"
  egress-ironproxy:
    image: "{{ egress_ironproxy_image }}"
  egress-firewall:
    image: "{{ egress_firewall_image }}"
""",
        encoding="utf-8",
    )


def test_saas_static_frontend_manifest_omits_frontend_service(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    template_path = tmp_path / "compose.yml.tpl"
    output_path = tmp_path / "compose.yml"
    _write_template(template_path)
    manifest_path.write_text(
        json.dumps(
            {
                "deploymentProfiles": {"saas": {"staticFrontend": True}},
                "images": {
                    "backend": "repo/backend:1",
                    "worker": "repo/worker:1",
                    "sharkd": "repo/sharkd:1",
                    "egress-ironproxy": "repo/ironproxy:1",
                    "egress-firewall": "repo/firewall:1",
                    "egress-dns": "repo/dns:1",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = render_compose.main(
        [
            "--manifest",
            str(manifest_path),
            "--template",
            str(template_path),
            "--output",
            str(output_path),
            "--profile",
            "saas",
        ]
    )

    assert rc == 0
    rendered = output_path.read_text(encoding="utf-8")
    assert "  frontend:" not in rendered
    assert "repo/backend:1" in rendered
    assert 'ports:\n      - "4448:4448"' in rendered
    assert 'expose:\n      - "4448"' not in rendered


def test_onprem_manifest_still_requires_frontend_image(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    template_path = tmp_path / "compose.yml.tpl"
    output_path = tmp_path / "compose.yml"
    _write_template(template_path)
    manifest_path.write_text(
        json.dumps(
            {
                "images": {
                    "backend": "repo/backend:1",
                    "worker": "repo/worker:1",
                    "sharkd": "repo/sharkd:1",
                    "egress-ironproxy": "repo/ironproxy:1",
                    "egress-firewall": "repo/firewall:1",
                    "egress-dns": "repo/dns:1",
                }
            }
        ),
        encoding="utf-8",
    )

    try:
        render_compose.main(
            [
                "--manifest",
                str(manifest_path),
                "--template",
                str(template_path),
                "--output",
                str(output_path),
            ]
        )
    except SystemExit as exc:
        assert "frontend_image" in str(exc)
    else:
        raise AssertionError("missing frontend image should fail on on-prem compose renders")


def test_rendered_onprem_compose_uses_journald_logging(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "compose.yml"
    template_path = Path(__file__).resolve().parents[1] / "templates" / "docker-compose.onprem.yml.tpl"
    manifest_path.write_text(
        json.dumps(
            {
                "images": {
                    "frontend": "repo/frontend:1",
                    "backend": "repo/backend:1",
                    "worker": "repo/worker:1",
                    "sharkd": "repo/sharkd:1",
                    "egress-ironproxy": "repo/ironproxy:1",
                    "egress-firewall": "repo/firewall:1",
                    "egress-dns": "repo/dns:1",
                }
            }
        ),
        encoding="utf-8",
    )

    rc = render_compose.main(
        [
            "--manifest",
            str(manifest_path),
            "--template",
            str(template_path),
            "--output",
            str(output_path),
        ]
    )

    assert rc == 0
    rendered = output_path.read_text(encoding="utf-8")
    assert "x-packetsafari-journald-logging:" in rendered
    assert "driver: journald" in rendered
    assert 'expose:\n      - "4448"' in rendered
    assert 'ports:\n      - "4448:4448"' not in rendered
    assert 'tag: "packetsafari/{{.Name}}/{{.ID}}"' in rendered
    assert rendered.count("logging: *packetsafari-journald-logging") >= 10
    assert '--save "3600 1 300 100 60 10000"' in rendered
    assert "--save 20 1" not in rendered


def test_onprem_backend_and_worker_share_persistent_codex_runtime(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "compose.yml"
    template_path = Path(__file__).resolve().parents[1] / "templates" / "docker-compose.onprem.yml.tpl"
    manifest_path.write_text(
        json.dumps(
            {
                "images": {
                    "frontend": "repo/frontend:1",
                    "backend": "repo/backend:1",
                    "worker": "repo/worker:1",
                    "sharkd": "repo/sharkd:1",
                    "egress-ironproxy": "repo/ironproxy:1",
                    "egress-firewall": "repo/firewall:1",
                    "egress-dns": "repo/dns:1",
                }
            }
        ),
        encoding="utf-8",
    )

    assert render_compose.main(
        [
            "--manifest",
            str(manifest_path),
            "--template",
            str(template_path),
            "--output",
            str(output_path),
        ]
    ) == 0

    rendered = output_path.read_text(encoding="utf-8")
    assert rendered.count("packetsafari-codexruntime:/var/lib/packetsafari/codex") == 3
    assert "packetsafari-codexruntime:" in rendered.split("\nvolumes:\n", 1)[1]
    storage_init = rendered.split("\n  storage-init:", 1)[1].split("\n  backend:", 1)[0]
    assert 'user: "0:0"' in storage_init
    assert "analysis/runtime/typed-shared" in storage_init
    for writable_root in (
        "upload/archive",
        "agent-visual-reports",
        "admin",
        "intelligence/suricata/rules",
        "runtime",
        "logs",
        "analysis/runtime/typed",
        "analysis/runtime/typed-securityscan",
    ):
        assert writable_root in storage_init
    assert "PACKETSAFARI_STORAGE_REPAIR_SUBDIRS" in storage_init
    assert "analysis/runtime/match-bitsets" in storage_init
    assert (
        "PACKETSAFARI_STORAGE_EXTERNAL_DIR=/var/lib/packetsafari/codex "
        'PACKETSAFARI_STORAGE_SUBDIRS="sqlite" PACKETSAFARI_STORAGE_REPAIR_SUBDIRS="." /usr/local/bin/setvolumepermissions.sh /'
    ) in rendered


def test_onprem_services_share_persistent_bounded_ids_cache(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "compose.yml"
    template_path = Path(__file__).resolve().parents[1] / "templates" / "docker-compose.onprem.yml.tpl"
    manifest_path.write_text(
        json.dumps(
            {
                "images": {
                    "frontend": "repo/frontend:1",
                    "backend": "repo/backend:1",
                    "worker": "repo/worker:1",
                    "sharkd": "repo/sharkd:1",
                    "egress-ironproxy": "repo/ironproxy:1",
                    "egress-firewall": "repo/firewall:1",
                    "egress-dns": "repo/dns:1",
                }
            }
        ),
        encoding="utf-8",
    )
    assert render_compose.main(
        [
            "--manifest",
            str(manifest_path),
            "--template",
            str(template_path),
            "--output",
            str(output_path),
        ]
    ) == 0

    rendered = output_path.read_text(encoding="utf-8")
    cache_path = "/storage/runtime/sharkd-ids-cache"
    storage_init = rendered.split("\n  storage-init:", 1)[1].split("\n  backend:", 1)[0]
    backend = rendered.split("\n  backend:", 1)[1].split("\n  worker:", 1)[0]
    worker = rendered.split("\n  worker:", 1)[1].split("\n  postgres:", 1)[0]
    sharkd = rendered.split("\n  sharkd:", 1)[1].split("\n  egress-dns:", 1)[0]

    assert "runtime/sharkd-ids-cache" in storage_init
    assert f"SHARKD_IDS_SHARED_CACHE_DIR: {cache_path}" in backend
    assert f"SHARKD_IDS_SHARED_CACHE_DIR: {cache_path}" in worker
    assert f"SHARKD_IDS_CACHE_DIR: {cache_path}" in sharkd
    assert f"SHARKD_IDS_SHARED_CACHE_DIR: {cache_path}" in sharkd
    assert "packetsafari-storage:/storage" in backend
    assert "packetsafari-storage:/storage" in worker
    assert "packetsafari-storage:/storage" in sharkd
