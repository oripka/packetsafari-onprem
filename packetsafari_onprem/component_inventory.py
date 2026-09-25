"""Bounded, allowlisted Docker observations; never expose inspect environment/secrets."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=5 if args[:2] == ['docker', 'exec'] else 15).stdout


def collect(compose, ops_version, run=_run):
    observed = datetime.now(timezone.utc).isoformat()
    result = {'schemaVersion': 1, 'observedAt': observed, 'opsVersion': ops_version,
              'status': 'unavailable', 'components': []}
    try:
        ids = list(dict.fromkeys(run([*compose, '--profile', '*', 'ps', '--all', '--quiet']).split()))
        if len(ids) > 128:
            raise ValueError('container_limit')
        if not ids:
            raise ValueError('no_containers')
        containers = json.loads(run(['docker', 'inspect', *ids]))
        image_ids = list(dict.fromkeys(item['Image'] for item in containers))
        images = {item['Id']: item for item in json.loads(run(['docker', 'image', 'inspect', *image_ids]))}
        metadata_reads = 0
        for item in containers:
            config = item.get('Config') or {}
            labels = config.get('Labels') or {}
            image = images.get(item['Image'], {})
            image_config = image.get('Config') or {}
            image_labels = image_config.get('Labels') or {}
            # Only known image build constants, never container runtime env.
            build_env = dict(value.split('=', 1) for value in image_config.get('Env') or [] if '=' in value)
            state = item.get('State') or {}
            codex_version = None
            service = labels.get('com.docker.compose.service', '')
            if metadata_reads < 6 and state.get('Running') and service.removesuffix('-green') in ('backend', 'worker', 'agent-cli-runner'):
                metadata_reads += 1
                try:
                    codex_version = run(['docker', 'exec', item['Id'], 'cat', '/app/build-metadata/codex/version.txt']).strip()[:128]
                except (OSError, subprocess.SubprocessError):
                    pass
            result['components'].append({
                'codexVersion': codex_version,
                'name': labels.get('com.docker.compose.service') or item.get('Name', '').lstrip('/'),
                'containerId': item.get('Id'), 'imageId': item.get('Image'),
                'imageReference': config.get('Image'), 'digests': image.get('RepoDigests') or [],
                'version': image_labels.get('org.opencontainers.image.version') or next(
                    (build_env[key] for key in ('PG_VERSION', 'REDIS_VERSION', 'NGINX_VERSION') if build_env.get(key)), None),
                'sourceCommit': image_labels.get('org.opencontainers.image.revision'),
                'builtAt': image.get('Created'), 'startedAt': state.get('StartedAt'),
                'state': state.get('Status'), 'health': (state.get('Health') or {}).get('Status', 'not_configured'),
            })
        result['status'] = 'observed'
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        result['status'] = 'unavailable'
        result['components'] = []
    return result


def publish(layout, compose, ops_version):
    from .deployment_proxy import atomic_write
    payload = collect(compose, ops_version)
    path = Path(layout.state_dir) / 'component-inventory.json'
    try:
        receipt = json.loads((Path(layout.state_dir) / 'last-deployment-receipt.json').read_text())
        payload['deployment'] = {key: receipt.get(key) for key in ('recordedAt', 'outcome')}
    except (OSError, ValueError, AttributeError):
        payload['deployment'] = {}
    atomic_write(path, json.dumps(payload, indent=2) + '\n')
    # This projection contains only public identities; the unprivileged API reads it.
    os.chmod(path, 0o644)
    return payload
