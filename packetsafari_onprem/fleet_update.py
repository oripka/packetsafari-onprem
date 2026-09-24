"""Two application generations sharing unchanged stateful infrastructure."""
from __future__ import annotations

import copy
import re
import subprocess
import time
import uuid
import sys
import shutil

from . import deployment_proxy as proxy
from . import workload_drain
from .rolling_update import read, save, compose_config

COHORT = ('backend', 'worker', 'agent-stream-gateway', 'agent-cli-runner', 'sharkd')
GREEN_IP = {'backend': '172.20.0.27', 'worker': '172.20.0.28',
            'agent-stream-gateway': '172.20.0.29', 'sharkd': '172.20.0.31'}


def pin_release(runtime, manifest_path):
    """Retain exact verified bytes and detached signature, never reserialize them."""
    from pathlib import Path
    source = Path(manifest_path)
    signature = Path(str(source) + '.sig')
    if signature.is_file():
        shutil.copy2(source, runtime.directory / 'pinned-release.json')
        shutil.copy2(signature, runtime.directory / 'pinned-release.json.sig')


def release_images(current, target, backup_mode):
    if backup_mode == 'inline':
        raise ValueError('Full-generation updates cannot use a quiescing inline backup')
    old, new = current.get('runtimeContract'), target.get('runtimeContract')
    if not isinstance(old, dict) or old != new or new.get('protocolVersion') != 1 or new.get('workerDrainVersion') != 1:
        raise ValueError('Release runtime/schema contracts differ or are missing; explicit maintenance bootstrap required')
    if not re.fullmatch('[a-f0-9]{64}', str(new.get('schemaInputs', ''))):
        raise ValueError('Release schema identity is invalid')
    for key in ('deploymentProfiles', 'requiredEnv', 'profileRequiredEnv', 'requiredEnvByProfile'):
        if current.get(key) != target.get(key):
            raise ValueError(f'{key} changed; no automatic maintenance fallback')
    from .rolling_update import image_ref
    before = {k: image_ref(v) for k, v in current['images'].items()}
    after = {k: image_ref(v) for k, v in target['images'].items()}
    for key in set(before) | set(after):
        if key not in COHORT and before.get(key) != after.get(key):
            raise ValueError(f'{key} image changed; no qualified overlap/drain contract')
    after.setdefault('agent-stream-gateway', after.get('backend'))
    for key in COHORT:
        if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}', str(after.get(key, ''))):
            raise ValueError(f'{key} must be present and pinned by digest')
    return {key: after[key] for key in COHORT}


def upgrade(layout, args, current, manifest, *, source, backup_mode, backup_proof, ops):
    from .rolling_update import Runtime, fingerprints
    images = release_images(current, manifest, backup_mode)
    if getattr(args, 'skip_health_check', False):
        raise ValueError('Generation updates require health checks')
    directory = layout.state_dir / 'rolling'
    runtime = Runtime(directory, layout.compose_file, ops._compose_base_command(layout))
    stack = read(runtime.stack_file)
    if 'fleetBases' not in stack:
        raise ValueError('Host has API-only activation; full-generation activation is required')
    recorded = stack.get('configurationFingerprint')
    from pathlib import Path
    if not recorded or fingerprints([Path(path) for path in recorded]) != recorded:
        raise ValueError('Frozen runtime configuration changed; explicit maintenance reconciliation required')
    target = copy.deepcopy(stack['fleetBases'][stack['active']])
    for service, image in images.items():
        target['services'][service]['image'] = image
    if source == 'manifest' and not getattr(args, 'skip_image_pull', False):
        if ops.deployment_profile(args) == 'saas':
            ops.ensure_ecr_credential_helper_ready(layout)
        for image in set(images.values()):
            subprocess.run(['docker', 'pull', image], check=True)
    metadata = directory / 'fleet-release.json'
    if runtime.journal_file.exists():
        saved = read(metadata)
        if saved['manifest'] != manifest:
            raise RuntimeError('An earlier release is still draining; repeat its exact release before applying another')
    else:
        pin_release(runtime, layout.target_release_manifest_path)
        snapshot = ops.snapshot_runtime(layout)
        if backup_proof:
            ops.record_external_backup_proof(snapshot, backup_proof)
        saved = {'manifest': manifest, 'snapshot': str(snapshot)}
        save(metadata, saved)
    result = {}
    def verify():
        ops.wait_for_health(timeout_seconds=getattr(args, 'health_timeout', 180))
        ops.wait_for_agent_stream_gateway(layout, timeout_seconds=getattr(args, 'health_timeout', 180))
        ops.wait_for_doctor_ok(args, timeout_seconds=getattr(args, 'health_timeout', 180))
    def commit(receipt):
        # The normal initializer also activates bundled intelligence. Do that
        # after old jobs drain, using its existing signed/idempotent content API;
        # never run the initializer's migrations against overlapping generations.
        container = runtime.container(read(runtime.stack_file)['active'])
        subprocess.run(['docker', 'exec', '--user', '0', '-e', 'PACKETSAFARI_STORAGE_SUBDIRS=capture-agent',
                        '-e', 'PACKETSAFARI_STORAGE_REPAIR_SUBDIRS=', container,
                        '/usr/local/bin/setvolumepermissions.sh', '/'], check=True)
        subprocess.run(['docker', 'exec', '--user', '0', '-e', 'PACKETSAFARI_SKIP_SERVICE_INIT=true',
                        container, 'python3', '/app/scripts/bootstrap_embedded_security_content.py'], check=True)
        subprocess.run(['docker', 'exec', '--user', '0', '-e', 'PACKETSAFARI_STORAGE_SUBDIRS=',
                        '-e', 'PACKETSAFARI_STORAGE_REPAIR_SUBDIRS=intelligence', container,
                        '/usr/local/bin/setvolumepermissions.sh', '/'], check=True)
        result.update(ops._promote_release(layout, manifest, Path(saved['snapshot']), source=source,
                      profile=ops.deployment_profile(args), backup_mode=backup_mode))
    outcome = deploy(runtime, target, verify=verify, commit=commit, timeout=getattr(args, 'health_timeout', 180))
    return {**result, **outcome}


def name(service, slot):
    return service + ('-green' if slot == 'backend-green' else '')


def configuration(stack, directory):
    bases = stack['fleetBases']
    active = stack['active']
    config = compose_config(bases[active], stack, directory)
    for slot, base in bases.items():
        green = slot == 'backend-green'
        for logical in COHORT:
            if logical not in base['services']:
                continue
            service = copy.deepcopy(base['services'][logical])
            service.pop('build', None)
            service['container_name'] = name(service.get('container_name', logical), slot)
            service['profiles'] = [] if slot == active else ['rolling-inactive']
            service['ports'] = []
            service['depends_on'] = {}
            if green and logical in GREEN_IP:
                for network in service['networks'].values():
                    network['ipv4_address'] = GREEN_IP[logical]
            environment = service.setdefault('environment', {})
            local_names = ','.join(name(item, slot) for item in COHORT)
            for key in ('NO_PROXY', 'no_proxy'):
                environment[key] = ','.join(filter(None, [environment.get(key) or environment.get('NO_PROXY'), local_names]))
            if logical in ('backend', 'worker'):
                environment.update(POSTGRES_AUTO_RUN_MIGRATIONS='false',
                                   PACKETSAFARI_INITIALIZE_DATABASE_ENABLED='false',
                                   PACKETSAFARI_CAPTURE_SHARKD_HOST=name('sharkd', slot),
                                   PACKETSAFARI_AGENT_GATEWAY_HOST=name('agent-stream-gateway', slot),
                                   PACKETSAFARI_WORKER_BACKEND_STARTUP_URL=f'http://{name("backend", slot)}/api/v2/system/live')
                environment['AI_AGENT_STREAM_GATEWAY_INTERNAL_URL'] = f'http://{name("agent-stream-gateway", slot)}:8091'
            # Each runner owns a different Unix socket and private workspace tree.
            # The old generation retains both until all of its jobs have finished.
            for mount in service.get('volumes', []):
                if green and mount.get('type') == 'volume' and mount.get('target') in ('/run/packetsafari-cli', '/workspaces'):
                    original = mount['source']
                    replacement = original + '-green'
                    volume = copy.deepcopy(config['volumes'][original])
                    if 'name' in volume:
                        volume['name'] += '-green'
                    config['volumes'][replacement] = volume
                    mount['source'] = replacement
            config['services'][name(logical, slot)] = service
    return config


def validate_shared(old, new):
    """No silent maintenance fallback for dependencies without an overlap contract."""
    for service in set(old['services']) | set(new['services']):
        if service in COHORT or service in ('init', 'storage-init'):
            continue
        if old['services'].get(service) != new['services'].get(service):
            raise ValueError(f'{service} changed and has no qualified overlap/drain contract; explicit maintenance required')
    for field in ('networks', 'volumes'):
        if old.get(field) != new.get(field):
            raise ValueError(f'Shared {field} changed; explicit maintenance required')


def ready(runtime, services, timeout=120):
    configured = read(runtime.compose_file)['services']
    expected = {service: proxy.inspect(configured[service]['image'])['Id'] for service in services}
    controller = read(runtime.stack_file)['proxyName']
    deadline = time.monotonic() + timeout
    while True:
        pending = []
        for service in services:
            container = runtime.container(service)
            info = proxy.inspect(container)
            if info['Image'] != expected[service]:
                raise RuntimeError(f'{service} is not running the intended image')
            state = info['State']
            if state.get('OOMKilled') or state.get('Dead') or not state['Running']:
                raise RuntimeError(f'{service} failed during generation preparation')
            if state.get('Health', {}).get('Status', 'healthy') != 'healthy':
                pending.append(service)
            if service in ('backend', 'backend-green'):
                endpoint = proxy.address(proxy.inspect(controller), info, 80)
                response = proxy.docker('exec', controller, 'wget', '-S', '-O', '/dev/null', '-T', '2',
                                        f'http://{endpoint}/api/v2/health', check=False)
                if response.returncode or re.findall(r'HTTP/\S+\s+(\d{3})', response.stderr) != ['200']:
                    pending.append(service)
            if service in ('worker', 'worker-green') and not workload_drain.worker_ready(container):
                pending.append(service)
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Generation readiness timed out: {", ".join(pending)}')
        time.sleep(.25)


def recover_preparing(runtime):
    journal = read(runtime.journal_file)
    if journal.get('mode') != 'fleet' or journal['phase'] != 'preparing':
        raise RuntimeError('Candidate workers may own jobs; repeat the normal update to resume their drain')
    slot = journal['candidate']
    configured = read(runtime.compose_file)['services']
    worker = name('worker', slot)
    if worker in configured and runtime.dc('ps', '-q', worker):
        raise RuntimeError('Candidate worker is running unexpectedly; retain its dependencies')
    unused = [name(service, slot) for service in COHORT if service != 'worker' and name(service, slot) in configured]
    if unused:
        runtime.dc('stop', *unused)
    save(runtime.stack_file, journal['oldStack'])
    runtime.render(journal['oldStack'])
    runtime.journal_file.replace(runtime.directory / f'fleet-{journal["id"]}-aborted.json')
    return {'status': 'rolled_back', 'message': 'Candidate failed before workers started; original generation kept serving'}


def deploy(runtime, target_base, *, verify=lambda: None, commit=lambda receipt: None, timeout=120):
    try:
        return _deploy(runtime, target_base, verify=verify, commit=commit, timeout=timeout)
    except BaseException:
        if runtime.journal_file.exists():
            journal = read(runtime.journal_file)
            if journal.get('mode') == 'fleet' and journal.get('phase') == 'preparing':
                recover_preparing(runtime)
        raise


def abort(runtime, timeout=120):
    """Return traffic, warm-drain candidate jobs, then use the normal retirement path."""
    if not runtime.journal_file.exists():
        return {'status': 'noop'}
    journal = read(runtime.journal_file)
    if journal.get('mode') != 'fleet':
        raise RuntimeError('This is not an application generation transaction')
    if journal['phase'] == 'preparing':
        return recover_preparing(runtime)
    if journal['phase'] in ('committing', 'committed'):
        raise RuntimeError('Release commit has begun; repeat update to finish, then deploy a signed rollback release')
    if not journal.get('rollback'):
        if 'candidateContainers' not in journal:
            # The process may have died just before/after Compose started workers.
            worker = runtime.dc('ps', '-a', '-q', name('worker', journal['candidate']))
            if not worker:
                journal['phase'] = 'preparing'
                save(runtime.journal_file, journal)
                return recover_preparing(runtime)
            workload_drain.begin_worker(worker)  # Refuse unproven/failed workers.
            journal['candidateContainers'] = {s: runtime.container(name(s, journal['candidate']))
                                               for s in COHORT if s in journal['targetBase']['services']}
            journal['candidateStarts'] = {s: proxy.inspect(c)['State']['StartedAt']
                                          for s, c in journal['candidateContainers'].items()}
        for service, container in journal['candidateContainers'].items():
            current = proxy.inspect(container)
            if (proxy.inspect(name(journal['targetBase']['services'][service].get('container_name', service), journal['candidate']))['Id'] != container
                    or current['State']['StartedAt'] != journal['candidateStarts'][service]):
                raise RuntimeError('Candidate identity changed; abort cannot prove ownership')
        journal.update(phase='rollback-switching', failure=journal.get('failure', 'Operator aborted update'))
        save(runtime.journal_file, journal)
    return deploy(runtime, journal['targetBase'], timeout=timeout)


def _deploy(runtime, target_base, *, verify=lambda: None, commit=lambda receipt: None, timeout=120):
    """Caller holds the deployment lock and verifies signed compatibility first."""
    if runtime.journal_file.exists():
        journal = read(runtime.journal_file)
        if journal.get('mode') != 'fleet' or journal['targetBase'] != target_base:
            raise RuntimeError('Another transaction is pending; do not replace its retained generations')
    else:
        stack = read(runtime.stack_file)
        old_base = stack.get('fleetBases', {}).get(stack['active'], read(runtime.directory / 'base.json'))
        validate_shared(old_base, target_base)
        if 'frontend' in old_base['services']:
            raise ValueError('Frontend container overlap is not yet qualified; explicit maintenance required')
        if not stack.get('sharkdPorts') and old_base['services']['sharkd'].get('ports'):
            raise ValueError('Sharkd public port must be transferred to the deployment proxy before fleet updates')
        if set(read(runtime.directory / 'proxy/state.json').get('retiringWorkers', [])) & proxy.workers(stack['proxyName']):
            raise RuntimeError('Previous proxy connections are still draining')
        active = stack['active']
        candidate = 'backend-green' if active == 'backend' else 'backend'
        worker = workload_drain.begin_worker(runtime.container(name('worker', active)))
        staged = copy.deepcopy(stack)
        staged['fleetBases'] = {active: old_base, candidate: target_base}
        for slot, base in staged['fleetBases'].items():
            staged['images'][slot] = base['services']['backend']['image']
        journal = {'mode': 'fleet', 'phase': 'preparing', 'oldStack': stack, 'staged': staged,
                   'id': uuid.uuid4().hex, 'retiringSlot': active,
                   'candidate': candidate, 'oldWorker': worker, 'targetBase': target_base,
                   'oldContainers': {s: runtime.container(name(s, active)) for s in COHORT if s in old_base['services']}}
        journal['oldStarts'] = {s: proxy.inspect(c)['State']['StartedAt'] for s, c in journal['oldContainers'].items()}
        save(runtime.journal_file, journal)
    staged, candidate = journal['staged'], journal['candidate']
    candidate_services = [name(s, candidate) for s in COHORT if s in target_base['services']]
    if journal['phase'] == 'preparing':
        print(f'Generation update: preparing {candidate}; serving generation and dependencies stay running', file=sys.stderr, flush=True)
        save(runtime.compose_file, configuration(staged, runtime.directory))
        dependencies = [s for s in candidate_services if s not in (name('backend', candidate), name('worker', candidate))]
        runtime.dc('up', '-d', '--no-deps', '--pull', 'never', '--force-recreate', *dependencies)
        ready(runtime, dependencies, timeout)
        runtime.dc('up', '-d', '--no-deps', '--pull', 'never', '--force-recreate', name('backend', candidate))
        ready(runtime, [name('backend', candidate)], timeout)
        if runtime.schema(journal['oldContainers']['backend']) != runtime.schema(runtime.container(candidate)):
            raise RuntimeError('Schema/model inputs changed; retained generations require compatible schema')
        # Save intent before candidate workers can consume messages. Any failure
        # after this boundary must retain both generations rather than kill jobs.
        journal['phase'] = 'starting-workers'
        save(runtime.journal_file, journal)
    if journal['phase'] == 'starting-workers':
        if journal.get('candidateContainers'):
            for service, container in journal['candidateContainers'].items():
                info = proxy.inspect(container)
                actual = runtime.container(name(service, candidate))
                if actual != container or info['State']['StartedAt'] != journal['candidateStarts'][service]:
                    raise RuntimeError('Candidate restarted or was replaced; retain dependencies and inspect before resuming')
        else:
            runtime.dc('up', '-d', '--no-deps', '--pull', 'never', name('worker', candidate))
            proxy.docker('update', '--restart=' + target_base['services']['worker'].get('restart', 'always'),
                         runtime.container(name('worker', candidate)))
            journal['candidateContainers'] = {s: runtime.container(name(s, candidate)) for s in COHORT if s in target_base['services']}
            journal['candidateStarts'] = {s: proxy.inspect(c)['State']['StartedAt'] for s, c in journal['candidateContainers'].items()}
            save(runtime.journal_file, journal)
        ready(runtime, candidate_services, timeout)
        workload_drain.begin_worker(runtime.container(name('worker', candidate)))
        journal['phase'] = 'switching'
        save(runtime.journal_file, journal)
    if journal['phase'] == 'switching':
        receipt = proxy.switch(runtime.directory / 'proxy', staged['proxyName'], runtime.container(candidate),
                               sharkd=runtime.container(name('sharkd', candidate)), drain_timeout=0)
        journal.update(phase='verifying', receipt=receipt)
        staged['active'] = candidate
        save(runtime.stack_file, staged)
        save(runtime.compose_file, configuration(staged, runtime.directory))
        save(runtime.journal_file, journal)
        print('Generation update: API and Sharkd traffic switched; verifying before draining old jobs', file=sys.stderr, flush=True)
    if journal['phase'] == 'verifying':
        try:
            verify()
            journal['phase'] = 'draining'
        except Exception as exc:
            journal.update(phase='rollback-switching', failure=str(exc))
        save(runtime.journal_file, journal)
    if journal['phase'] == 'rollback-switching':
        original = journal['oldStack']['active']
        if not proxy.inspect(journal['oldContainers']['worker'])['State']['Running']:
            runtime.dc('up', '-d', '--no-deps', '--pull', 'never', name('worker', original))
            proxy.docker('update', '--restart=always', runtime.container(name('worker', original)))
            ready(runtime, [name('worker', original)], timeout)
        receipt = proxy.switch(runtime.directory / 'proxy', staged['proxyName'], journal['oldContainers']['backend'],
                               sharkd=journal['oldContainers']['sharkd'], drain_timeout=0)
        staged['active'] = original
        journal.update(phase='draining', rollback=True, receipt=receipt, retiringSlot=candidate,
                       oldContainers=journal['candidateContainers'],
                       oldStarts=journal['candidateStarts'],
                       oldWorker=workload_drain.begin_worker(journal['candidateContainers']['worker']))
        save(runtime.stack_file, staged)
        save(runtime.compose_file, configuration(staged, runtime.directory))
        save(runtime.journal_file, journal)
    if journal['phase'] == 'draining':
        def save_worker(receipt):
            journal['oldWorker'] = receipt
            save(runtime.journal_file, journal)
        receipt = workload_drain.request_worker(journal['oldWorker'], save_worker)
        result = workload_drain.wait_worker(receipt, timeout)
        save_worker(result)
        connections = set(read(runtime.directory / 'proxy/state.json').get('retiringWorkers', [])) & proxy.workers(staged['proxyName'])
        if result['status'] != 'drained' or connections:
            return {'status': 'draining', 'worker': result['status'], 'retiringProxyWorkers': sorted(connections),
                    'retainedServices': list(journal['oldContainers']),
                    'message': 'Old generation and dependencies retained. Repeat update to resume.'}
        if not journal.get('rollback'):
            try:
                verify()
            except Exception as exc:
                journal.update(phase='rollback-switching', failure=str(exc))
                save(runtime.journal_file, journal)
                return deploy(runtime, target_base, verify=verify, commit=commit, timeout=timeout)
        journal['phase'] = 'committing'
        save(runtime.journal_file, journal)
    if journal['phase'] == 'committing':
        if not journal.get('rollback'):
            commit(journal['receipt'])
        journal['phase'] = 'committed'
        save(runtime.journal_file, journal)
    for service, container in journal['oldContainers'].items():
        if service == 'worker':
            continue
        info = proxy.inspect(container)
        old_name = name(journal['staged']['fleetBases'][journal['retiringSlot']]['services'][service].get('container_name', service), journal['retiringSlot'])
        if proxy.inspect(old_name)['Id'] != container:
            raise RuntimeError('Old generation identity changed; refusing cleanup')
        if info['State']['StartedAt'] != journal['oldStarts'][service]:
            raise RuntimeError('Old dependency restarted; its idle state is no longer proven')
        if info['State']['Running']:
            runtime.dc('stop', name(service, journal['retiringSlot']))
    save(runtime.directory / 'base.json', staged['fleetBases'][staged['active']])
    runtime.journal_file.replace(runtime.directory / f'fleet-{journal["id"]}.json')
    print('Generation update: old jobs and connections drained; retired application containers stopped', file=sys.stderr, flush=True)
    return {'status': 'rolled_back' if journal.get('rollback') else 'ok', 'scope': 'application-generation',
            'services': {service: {'active': name(service, staged['active']),
                                   'image': staged['fleetBases'][staged['active']]['services'][service]['image'],
                                   'retiredContainer': container, 'retiredStatus': 'stopped'}
                         for service, container in journal['oldContainers'].items()},
            'rollingUpdate': journal['receipt'], **({'error': journal['failure']} if journal.get('rollback') else {})}
