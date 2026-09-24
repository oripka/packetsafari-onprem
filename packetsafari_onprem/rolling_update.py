"""Compose API-only rolling updates. No schema migration or worker replacement."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from . import deployment_proxy as proxy


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    proxy.atomic_write(Path(path), json.dumps(value, indent=2))


def enabled(layout):
    return (layout.state_dir / 'rolling' / 'stack.json').exists()


def active_service(layout, service='backend'):
    if not enabled(layout):
        return service
    stack = read(layout.state_dir / 'rolling' / 'stack.json')
    if service == 'backend':
        return stack['active']
    from .fleet_update import COHORT, name
    return name(service, stack['active']) if 'fleetBases' in stack and service in COHORT else service


def image_ref(value):
    return value.get('image', '') if isinstance(value, dict) else value or ''


def fingerprints(paths):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            for path in paths}


def validate_release(current, target, backup_mode):
    if backup_mode == 'inline':
        raise ValueError('Rolling updates require a verified external backup or an explicit unbacked-upgrade acknowledgement; inline backup quiesces services.')
    compatible = target.get('rollingUpdate', {}).get('compatibleFrom', [])
    if not isinstance(compatible, list) or current.get('version') not in compatible:
        raise ValueError('Signed target manifest must declare rollingUpdate.compatibleFrom for the exact installed version')
    previous, upcoming = current.get('images', {}), target.get('images', {})
    for key in set(previous) | set(upcoming):
        if key == 'backend':
            continue
        old = image_ref(previous.get(key))
        new = image_ref(upcoming.get(key))
        if key == 'agent-stream-gateway':
            old = old or image_ref(previous.get('backend'))
        if old != new:
            raise ValueError(f'Rolling API update cannot change {key}; use the maintenance deployment path')
    # Gateway historically inherits backend; it must now be independently frozen.
    old_gateway = image_ref(previous.get('agent-stream-gateway')) or image_ref(previous.get('backend'))
    new_gateway = image_ref(upcoming.get('agent-stream-gateway')) or image_ref(upcoming.get('backend'))
    if old_gateway != new_gateway:
        raise ValueError('Rolling API update must retain the agent-stream-gateway image explicitly')
    if current.get('deploymentProfiles') != target.get('deploymentProfiles'):
        raise ValueError('Rolling update cannot change deployment profiles')
    for field in ['requiredEnv', 'requiredEnvByProfile']:
        if current.get(field) != target.get(field):
            raise ValueError(f'Rolling update cannot change {field}')
    backend = image_ref(upcoming.get('backend'))
    if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}', backend):
        raise ValueError('Rolling target backend must be pinned by registry digest')
    return backend


def compose_config(base, stack, directory):
    config = copy.deepcopy(base)
    services = config['services']
    blue = services['backend']
    network = next(iter(blue['networks']))
    if len(blue['networks']) != 1:
        raise ValueError('Rolling backend requires exactly one governed network')
    green = copy.deepcopy(blue)
    green['container_name'] = blue.get('container_name', 'packetsafari-backend') + '-green'
    green['networks'][network] = {'ipv4_address': '172.20.0.27'}
    for name, service in [('backend', blue), ('backend-green', green)]:
        service['image'] = stack['images'][name]
        service.pop('build', None)
        service['ports'] = []
        service['profiles'] = [] if stack['active'] == name else ['rolling-inactive']
        service.setdefault('environment', {}).update({'POSTGRES_AUTO_RUN_MIGRATIONS': 'false',
                                                     'PACKETSAFARI_INITIALIZE_DATABASE_ENABLED': 'false'})
        services[name] = service
    services['deployment-proxy'] = {
        'image': stack['proxyImage'], 'container_name': stack['proxyName'],
        'restart': 'unless-stopped', 'networks': {network: {'ipv4_address': '172.20.0.26'}},
        'ports': stack['ports'], 'volumes': [{'type': 'bind', 'source': str(directory / 'proxy'),
                 'target': '/etc/packetsafari-proxy', 'read_only': True}],
        'command': ['nginx', '-g', 'daemon off;', '-c', '/etc/packetsafari-proxy/nginx.conf'],
    }
    if stack.get('sharkdPorts'):
        services['deployment-proxy']['ports'] = [*stack['ports'], *stack['sharkdPorts']]
        services['sharkd']['ports'] = []
    if 'frontend' in services:
        services['frontend'].setdefault('environment', {})['NUXT_INTERNAL_API_BASE'] = 'http://deployment-proxy:8080'
    return config


class Runtime:
    def __init__(self, directory, compose_file, compose_command):
        self.directory = Path(directory)
        self.compose_file = Path(compose_file)
        self.command = compose_command
        self.stack_file = self.directory / 'stack.json'
        self.journal_file = self.directory / 'transaction.json'

    def dc(self, *args):
        result = subprocess.run([*self.command, *args], text=True, capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError(f'Compose {args[0]} failed: {result.stderr[-1500:]}')
        return result.stdout.strip()

    def container(self, name):
        result = self.dc('ps', '-q', name)
        if not result or '\n' in result:
            raise RuntimeError(f'Expected one running {name} container')
        return result

    def render(self, stack):
        if 'fleetBases' in stack:
            from .fleet_update import configuration
            config = configuration(stack, self.directory)
        else:
            config = compose_config(read(self.directory / 'base.json'), stack, self.directory)
        save(self.compose_file, config)
        self.dc('config', '--quiet')

    def schema(self, container):
        script = """import hashlib,pathlib
root=pathlib.Path('/app')
files=sorted((root/'alembic').rglob('*.py'))+[root/'packetsafari/storage/sql/models.py']
assert len(files)>1 and all(p.is_file() for p in files), 'schema inputs missing'
h=hashlib.sha256()
for p in files:
 h.update(str(p.relative_to(root)).encode()+b'\\0'); h.update(p.read_bytes()); h.update(b'\\0')
print(h.hexdigest())
"""
        return proxy.docker('exec', container, 'python3', '-c', script).stdout.strip()

    def deploy(self, image, *, verify=lambda: None, commit=lambda receipt: None, timeout=120, context=None):
        """Caller holds deployment lock and verifies signed release/backup policy first."""
        stack = read(self.stack_file)
        if 'fleetBases' in stack:
            from .fleet_update import deploy
            target = copy.deepcopy(stack['fleetBases'][stack['active']])
            target['services']['backend']['image'] = image
            return deploy(self, target, verify=verify, commit=commit, timeout=timeout)
        if self.journal_file.exists():
            raise RuntimeError('An unfinished rolling transaction exists; recover it before another update')
        active = stack['active']
        inactive = 'backend-green' if active == 'backend' else 'backend'
        controller = stack['proxyName']
        proxy_state = read(self.directory / 'proxy/state.json')
        if set(proxy_state.get('retiringWorkers', [])) & proxy.workers(controller):
            raise RuntimeError('Previous connections are still draining; no slot can be replaced')
        old_container = self.container(active)
        journal = {'phase': 'preparing', 'oldStack': stack, 'candidate': inactive, 'image': image,
                   'oldContainer': old_container, 'context': context or {}}
        save(self.journal_file, journal)
        try:
            print(f'Rolling API: starting {inactive}; {active} continues serving', file=sys.stderr, flush=True)
            staged = copy.deepcopy(stack)
            staged['images'][inactive] = image
            self.render(staged)
            self.dc('up', '-d', '--no-deps', '--pull', 'never', inactive)
            candidate = self.container(inactive)
            journal['candidateContainer'] = candidate
            save(self.journal_file, journal)
            if self.schema(old_container) != self.schema(candidate):
                raise RuntimeError('Schema/model inputs changed; rolling path does not run migrations')
            print('Rolling API: waiting for candidate readiness, then switching and draining', file=sys.stderr, flush=True)
            receipt = proxy.switch(self.directory / 'proxy', controller, candidate,
                                   ready_timeout=timeout, drain_timeout=timeout)
            journal.update(phase='switched', receipt=receipt)
            save(self.journal_file, journal)
            staged['active'] = inactive
            save(self.stack_file, staged)
            self.render(staged)
            if receipt['status'] != 'drained':
                # Keep both versions alive; retry/recovery must not recreate either.
                return {'status': 'draining', 'rollingUpdate': receipt}
            print('Rolling API: traffic switched; verifying before release commit', file=sys.stderr, flush=True)
            verify()
            journal['phase'] = 'committing'
            save(self.journal_file, journal)
            commit(receipt)
            journal['phase'] = 'committed'
            save(self.journal_file, journal)
            self.dc('stop', active)
            self.journal_file.unlink()
            print(f'Rolling API: {inactive} active; {active} stopped', file=sys.stderr, flush=True)
            return {'status': 'ok', 'rollingUpdate': receipt}
        except BaseException:
            if read(self.journal_file)['phase'] not in ('committing', 'committed'):
                self.recover()
            raise

    def recover(self):
        """Roll back traffic first. Never restart or overwrite the serving old slot."""
        if not self.journal_file.exists():
            return {'status': 'noop'}
        journal = read(self.journal_file)
        if journal.get('mode') == 'fleet':
            from .fleet_update import recover_preparing
            return recover_preparing(self)
        if journal['phase'] == 'committing':
            raise RuntimeError('Release metadata commit was interrupted; reconcile installed manifest before recovery')
        if journal['phase'] == 'committed':
            self.dc('stop', journal['oldStack']['active'])
            self.journal_file.unlink()
            return {'status': 'committed'}
        old = journal['oldStack']
        with (self.directory / 'proxy/lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proxy.reconcile(self.directory / 'proxy', old['proxyName'])
        current = read(self.directory / 'proxy/state.json')
        old_container = journal['oldContainer']
        # The proxy primitive verifies its disk/live generation before reloading.
        if current.get('containerId') != old_container:
            receipt = proxy.switch(self.directory / 'proxy', old['proxyName'], old_container, drain_timeout=0)
            if receipt['status'] == 'draining':
                return {'status': 'draining', 'rollingUpdate': receipt}
        if set(read(self.directory / 'proxy/state.json').get('retiringWorkers', [])) & proxy.workers(old['proxyName']):
            return {'status': 'draining'}
        save(self.stack_file, old)
        self.render(old)
        self.dc('stop', journal['candidate'])
        self.journal_file.unlink()
        return {'status': 'rolled_back'}


def upgrade(layout, args, manifest, *, source, backup_mode, backup_proof, ops):
    """Called only after the normal signature, profile, entitlement and env gates."""
    current = read(layout.release_manifest_path)
    if manifest.get('runtimeContract'):
        from .fleet_update import upgrade as fleet_upgrade
        return fleet_upgrade(layout, args, current, manifest, source=source,
                             backup_mode=backup_mode, backup_proof=backup_proof, ops=ops)
    image = validate_release(current, manifest, backup_mode)
    if getattr(args, 'skip_health_check', False):
        raise ValueError('Rolling updates cannot skip health checks')
    directory = layout.state_dir / 'rolling'
    runtime = Runtime(directory, layout.compose_file, ops._compose_base_command(layout))
    recorded = read(runtime.stack_file).get('configurationFingerprint')
    if not recorded or fingerprints([Path(path) for path in recorded]) != recorded:
        raise ValueError('Runtime configuration changed since proxy activation; maintenance reconciliation required')
    if runtime.journal_file.exists():
        raise RuntimeError('Unfinished rolling update; run deployment-proxy recover before retrying')
    snapshot = ops.snapshot_runtime(layout)
    if backup_proof:
        ops.record_external_backup_proof(snapshot, backup_proof)
    if source == 'manifest' and not getattr(args, 'skip_image_pull', False):
        if ops.deployment_profile(args) == 'saas':
            ops.ensure_ecr_credential_helper_ready(layout)
        subprocess.run(['docker', 'pull', image], check=True)
    result = {}

    def verify():
        ops.wait_for_health(timeout_seconds=getattr(args, 'health_timeout', 180))
        ops.wait_for_doctor_ok(args, timeout_seconds=getattr(args, 'health_timeout', 180))

    def commit(receipt):
        result.update(ops._promote_release(layout, manifest, snapshot, source=source,
                                          profile=ops.deployment_profile(args), backup_mode=backup_mode))
        state = ops._read_json(layout.deployment_state_path, {})
        state['rollback']['note'] = 'API-only rolling update; no migrations ran. Use a signed compatible rollback release or recover an unfinished transaction.'
        ops._write_json(layout.deployment_state_path, state)

    try:
        receipt = runtime.deploy(image, verify=verify, commit=commit,
                                 timeout=getattr(args, 'health_timeout', 180),
                                 context={'metadataSnapshot': str(snapshot)})
    except BaseException:
        # No migration ran. Restore metadata only; never stop shared services.
        phase = read(runtime.journal_file)['phase'] if runtime.journal_file.exists() else ''
        if phase not in ('committing', 'committed'):
            ops._restore_metadata_snapshot(layout, snapshot)
            runtime.render(read(runtime.stack_file))
        raise
    return {**result, **receipt}


def manage_host(args):
    from . import operations as ops
    layout = ops.runtime_layout(args.runtime_root, args.container_runtime_root)
    directory = layout.state_dir / 'rolling'
    runtime = Runtime(directory, layout.compose_file, ops._compose_base_command(layout))
    with ops.upgrade_lock(layout):
        if args.action == 'recover':
            journal = read(runtime.journal_file) if runtime.journal_file.exists() else {}
            if journal.get('phase') == 'committing':
                # Old slot cannot have been stopped before the durable committed phase.
                journal['phase'] = 'switched'
                save(runtime.journal_file, journal)
            result = runtime.recover()
            snapshot = journal.get('context', {}).get('metadataSnapshot')
            if result['status'] == 'rolled_back' and snapshot:
                ops._restore_metadata_snapshot(layout, Path(snapshot))
                runtime.render(read(runtime.stack_file))
            return result
        if enabled(layout):
            raise ValueError('Rolling updates already enabled')
        if not args.ingress_policy:
            raise ValueError('Host activation requires an explicit --ingress-policy')
        ingress = proxy.ingress_policy(read(args.ingress_policy))
        if args.profile == 'saas' and ingress['mode'] != 'cloudfront-https':
            raise ValueError('SaaS activation requires cloudfront-https ingress policy')
        manifest = read(layout.release_manifest_path)
        if not args.manifest:
            raise ValueError('Enable requires --manifest pointing to the signed installed release')
        verified = read(ops.materialize_verified_release_manifest(layout, args.manifest, args))
        if verified != manifest:
            raise ValueError('Enable manifest does not match the installed release')
        ops.validate_manifest_profile(manifest, expected_profile=args.profile)
        proxy_image = image_ref(manifest.get('images', {}).get('deployment-proxy'))
        if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}', proxy_image):
            raise ValueError('Installed signed release must include a digest-pinned deployment-proxy image')
        base = json.loads(subprocess.run([*ops._compose_base_command(layout), 'config', '--format', 'json'],
                        text=True, capture_output=True, check=True).stdout)
        firewall = layout.runtime_root / 'configuration/egress-firewall/run_egress_firewall.sh'
        policy = firewall.read_text()
        if 'backend-green:172.20.0.27' not in policy or 'deployment-proxy:172.20.0.26' not in policy:
            raise ValueError('Install the current governed green-slot and proxy firewall policy before activation')
        if manifest.get('runtimeContract'):
            if 'frontend' in base['services']:
                raise ValueError('Container-fronted deployments do not yet have a qualified generation drain contract')
            from .workload_drain import begin_worker
            begin_worker(runtime.container('worker'))
            if any(value not in policy for value in ('worker-green:172.20.0.28', 'agent-stream-gateway-green:172.20.0.29', 'sharkd-green:172.20.0.31')):
                raise ValueError('Install the full-generation firewall policy before activation')
        result = bootstrap(runtime, base, proxy_name='packetsafari-deployment-proxy', proxy_image=proxy_image, ingress=ingress)
        stack = read(runtime.stack_file)
        if manifest.get('runtimeContract'):
            stack['fleetBases'] = {'backend': base, 'backend-green': copy.deepcopy(base)}
        stack['configurationFingerprint'] = fingerprints([
            layout.runtime_env_path, layout.runtime_sizing_env_path, layout.compose_sizing_file,
            firewall, layout.production_egress_allowlist_path, layout.production_ironproxy_config_path])
        save(runtime.stack_file, stack)
        runtime.render(stack)
        return result


def bootstrap(runtime, base, *, proxy_name, proxy_image=proxy.PROXY_IMAGE, ingress=None):
    """One-time port transfer; caller owns the runtime lock. Existing data is shared."""
    directory = runtime.directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if runtime.stack_file.exists():
        raise ValueError('Rolling runtime already enabled')
    backend = base['services']['backend']
    ports = [dict(port, target=8080) for port in backend.get('ports', []) if int(port['target']) == 80]
    if not ports:
        raise ValueError('Backend must publish its existing HTTP port before proxy activation')
    image = backend['image']
    stack = {'active': 'backend', 'images': {'backend': image, 'backend-green': image},
             'ports': ports, 'proxyImage': proxy_image, 'proxyName': proxy_name}
    sharkd_ports = base['services'].get('sharkd', {}).get('ports', [])
    if sharkd_ports:
        if any(int(port['target']) != 4448 for port in sharkd_ports):
            raise ValueError('Unexpected Sharkd published port; cannot transfer it safely')
        stack['sharkdPorts'] = sharkd_ports
    save(directory / 'base.json', base)
    proxy.initialize(directory / 'proxy', ingress)
    before = runtime.compose_file.read_text()
    save(runtime.stack_file, stack)
    started = False
    try:
        runtime.render(stack)
        if 'egress-firewall' in base['services']:
            runtime.dc('restart', 'egress-firewall')
        started = True
        if sharkd_ports:
            runtime.dc('up', '-d', '--no-deps', 'sharkd')
        runtime.dc('up', '-d', '--no-deps', 'backend', 'deployment-proxy')
        # The proxy master may still be starting after Compose returns.
        deadline = time.monotonic() + 15
        while True:
            try:
                proxy.generation(proxy_name)
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
        receipt = proxy.switch(directory / 'proxy', proxy_name, runtime.container('backend'),
                               **({'sharkd': runtime.container('sharkd')} if sharkd_ports else {}))
        if 'frontend' in base['services']:
            runtime.dc('up', '-d', '--no-deps', 'frontend')
        return {'status': 'enabled', 'rollingUpdate': receipt}
    except BaseException:
        proxy.atomic_write(runtime.compose_file, before)
        if started:
            proxy.docker('stop', proxy_name, check=False)
            runtime.dc('up', '-d', '--no-deps', 'backend')
            if sharkd_ports:
                runtime.dc('up', '-d', '--no-deps', 'sharkd')
        runtime.stack_file.unlink()
        raise
