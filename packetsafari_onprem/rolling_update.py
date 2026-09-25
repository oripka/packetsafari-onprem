"""Compose topology, activation and recovery of legacy API-only transactions."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

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


def activation_base(compose_command):
    # Sizing overlays retain services in dormant profiles. Snapshot all of them.
    result = subprocess.run([*compose_command, '--profile', '*', 'config', '--format', 'json'],
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


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
        self.validate_config(config)
        save(self.compose_file, config)

    def validate_config(self, config):
        """Validate every merged profile without replacing the serving Compose file."""
        self.compose_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', prefix='.rolling-preflight-', suffix='.yml',
                                         dir=self.compose_file.parent, delete=False) as handle:
            json.dump(config, handle)
            candidate = Path(handle.name)
        try:
            command = [str(candidate) if part == str(self.compose_file) else part for part in self.command]
            if command == self.command:
                raise ValueError('Compose command does not reference the active Compose file')
            result = subprocess.run([*command, '--profile', '*', 'config', '--quiet'],
                                    text=True, capture_output=True, timeout=180)
            if result.returncode:
                raise RuntimeError(f'Merged Compose preflight failed: {result.stderr[-1500:]}')
        finally:
            candidate.unlink(missing_ok=True)

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
        raise RuntimeError('Legacy API-only mode is recovery-only; use dev rebuild or a maintenance upgrade to migrate')

    def recover(self):
        """Roll back traffic first. Never restart or overwrite the serving old slot."""
        if not self.journal_file.exists():
            return {'status': 'noop'}
        journal = read(self.journal_file)
        if journal.get('mode') == 'fleet':
            from .fleet_update import abort
            return abort(self)
        if journal.get('mode') == 'maintenance':
            raise RuntimeError('Maintenance may have changed schema; repeat update to resume. Database restoration requires its backup procedure.')
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
    from .fleet_update import upgrade as fleet_upgrade
    return fleet_upgrade(layout, args, read(layout.release_manifest_path), manifest, source=source,
                         backup_mode=backup_mode, backup_proof=backup_proof, ops=ops)


def manage_host(args):
    from . import operations as ops
    layout = ops.runtime_layout(args.runtime_root, args.container_runtime_root)
    directory = layout.state_dir / 'rolling'
    runtime = Runtime(directory, layout.compose_file, ops._compose_base_command(layout))
    with ops.upgrade_lock(layout):
        if args.action == 'recover':
            journal = read(runtime.journal_file) if runtime.journal_file.exists() else {}
            if journal.get('mode') not in ('fleet', 'maintenance') and journal.get('phase') == 'committing':
                # Old slot cannot have been stopped before the durable committed phase.
                journal['phase'] = 'switched'
                save(runtime.journal_file, journal)
            result = runtime.recover()
            snapshot = journal.get('context', {}).get('metadataSnapshot')
            if result['status'] == 'rolled_back' and snapshot:
                ops._restore_metadata_snapshot(layout, Path(snapshot))
                runtime.render(read(runtime.stack_file))
            return result
        if not args.ingress_policy:
            raise ValueError('Host activation requires an explicit --ingress-policy')
        ingress = proxy.ingress_policy(read(args.ingress_policy))
        if args.profile == 'saas' and ingress['mode'] != 'cloudfront-https':
            raise ValueError('SaaS activation requires cloudfront-https ingress policy')
        if enabled(layout) and not recover_incomplete_bootstrap(runtime, layout, ingress):
            raise ValueError('Rolling updates already enabled')
        manifest = read(layout.release_manifest_path)
        if not manifest.get('runtimeContract'):
            raise ValueError('New activation requires a drain-capable generation release; API-only activation is retired')
        if not args.manifest:
            raise ValueError('Enable requires --manifest pointing to the signed installed release')
        verified = read(ops.materialize_verified_release_manifest(layout, args.manifest, args))
        if verified != manifest:
            raise ValueError('Enable manifest does not match the installed release')
        ops.validate_manifest_profile(manifest, expected_profile=args.profile)
        proxy_image = image_ref(manifest.get('images', {}).get('deployment-proxy'))
        if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}', proxy_image):
            raise ValueError('Installed signed release must include a digest-pinned deployment-proxy image')
        base = activation_base(ops._compose_base_command(layout))
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


def bootstrap_stack(base, *, proxy_name, proxy_image):
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
    return stack


def preflight_activation(layout, manifest, ops):
    """Check the first-adoption topology before maintenance stops the old stack."""
    from .fleet_update import configuration
    runtime = Runtime(layout.state_dir / 'rolling', layout.compose_file, ops._compose_base_command(layout))
    base = activation_base(runtime.command)
    stack = bootstrap_stack(base, proxy_name='packetsafari-deployment-proxy',
                            proxy_image=image_ref(manifest.get('images', {}).get('deployment-proxy')))
    runtime.validate_config(compose_config(base, stack, runtime.directory))
    full = {**stack, 'fleetBases': {'backend': base, 'backend-green': copy.deepcopy(base)}}
    runtime.validate_config(configuration(full, runtime.directory))


def initialize_bootstrap_proxy(directory, ingress):
    state_file = directory / 'state.json'
    config_file = directory / 'nginx.conf'
    if not state_file.exists() and not config_file.exists():
        proxy.initialize(directory, ingress)
        return
    if not state_file.is_file() or not config_file.is_file():
        raise RuntimeError('Incomplete proxy bootstrap state; inspect before retrying')
    state = read(state_file)
    expected = proxy.configuration(None, 'unconfigured', ingress)
    if (state.get('generation') != 'unconfigured' or
            state.get('ingressPolicy') != proxy.ingress_policy(ingress) or
            config_file.read_text() != expected or
            state.get('configSha256') != hashlib.sha256(expected.encode()).hexdigest()):
        raise RuntimeError('Proxy bootstrap state changed; refusing to overwrite it')


def recover_incomplete_bootstrap(runtime, layout, ingress):
    """Retain and retire only a proven pre-port-transfer activation marker."""
    activation = read(layout.state_dir / 'rolling/activation.json') if (layout.state_dir / 'rolling/activation.json').exists() else {}
    if activation.get('phase') != 'app-installed':
        return False
    stack = read(runtime.stack_file)
    if stack.get('fleetBases') or stack.get('active') != 'backend':
        return False
    if not (runtime.directory / 'proxy/state.json').is_file() or not (runtime.directory / 'proxy/nginx.conf').is_file():
        return False
    initialize_bootstrap_proxy(runtime.directory / 'proxy', ingress)
    if proxy.docker('inspect', stack['proxyName'], check=False).returncode == 0:
        return False
    serving = activation_base(runtime.command)['services']
    if 'deployment-proxy' in serving or not serving.get('backend', {}).get('ports'):
        return False
    retained = runtime.directory / f'bootstrap-incomplete-{uuid.uuid4().hex}.json'
    runtime.stack_file.replace(retained)
    return True


def bootstrap(runtime, base, *, proxy_name, proxy_image=proxy.PROXY_IMAGE, ingress=None):
    """One-time port transfer; caller owns the runtime lock. Existing data is shared."""
    directory = runtime.directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if runtime.stack_file.exists():
        raise ValueError('Rolling runtime already enabled')
    stack = bootstrap_stack(base, proxy_name=proxy_name, proxy_image=proxy_image)
    sharkd_ports = stack.get('sharkdPorts')
    runtime.validate_config(compose_config(base, stack, directory))
    if 'frontend' not in base['services']:
        from .fleet_update import configuration
        full = {**stack, 'fleetBases': {'backend': base, 'backend-green': copy.deepcopy(base)}}
        runtime.validate_config(configuration(full, directory))
    save(directory / 'base.json', base)
    initialize_bootstrap_proxy(directory / 'proxy', ingress)
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
