"""Explicit maintenance using the same runtime, proxy and transaction journal."""
from __future__ import annotations

import copy
import time
import uuid

from . import deployment_proxy as proxy, workload_drain
from .fleet_update import COHORT, name, ready
from .rolling_update import read, save


def restore_before_changes(runtime, journal, timeout):
    """A failed drain is reversible only before any service/migration changes."""
    if journal['phase'] not in ('quiescing', 'draining', 'restoring'):
        raise RuntimeError('Cannot restore serving traffic after maintenance changes began')
    journal['phase'] = 'restoring'
    save(runtime.journal_file, journal)
    stack = journal['oldStack']
    active = stack['active']
    worker = runtime.container(name('worker', active))
    policy = journal['oldWorker'].get('restartPolicy', {'Name': 'always'})
    restart = policy.get('Name') or 'no'
    if restart == 'on-failure' and policy.get('MaximumRetryCount'):
        restart += ':' + str(policy['MaximumRetryCount'])
    proxy.docker('update', '--restart=' + restart, worker)
    proxy.docker('start', worker)
    ready(runtime, [name(service, active) for service in COHORT], timeout)
    proxy.switch(runtime.directory / 'proxy', stack['proxyName'],
                 runtime.container(active), sharkd=runtime.container(name('sharkd', active)),
                 drain_timeout=0)
    journal['phase'] = 'aborted-before-changes'
    save(runtime.journal_file, journal)
    runtime.journal_file.replace(runtime.directory / f'maintenance-aborted-{journal["id"]}.json')


def deploy(runtime, *, prepare, start, verify=lambda: None, commit=lambda: None,
           target=None, proxy_image=None, timeout=120):
    """Drain first; migrations/infra may interrupt service and are never auto-undone."""
    if runtime.journal_file.exists():
        journal = read(runtime.journal_file)
        if journal.get('mode') != 'maintenance':
            raise RuntimeError('Finish or abort the application update before maintenance')
    else:
        stack = read(runtime.stack_file)
        if 'fleetBases' not in stack:
            raise RuntimeError('Legacy activation requires the documented one-time bootstrap')
        inactive = 'backend-green' if stack['active'] == 'backend' else 'backend'
        if runtime.dc('ps', '-q', name('worker', inactive)):
            raise RuntimeError('Inactive workers still exist; finish their deployment first')
        worker = workload_drain.begin_worker(runtime.container(name('worker', stack['active'])))
        journal = {'mode': 'maintenance', 'id': uuid.uuid4().hex, 'phase': 'quiescing',
                   'oldStack': stack, 'oldWorker': worker, 'targetBase': target,
                   'proxyImage': proxy_image or stack['proxyImage']}
        save(runtime.journal_file, journal)
    stack = journal['oldStack']
    if journal['phase'] == 'restoring':
        restore_before_changes(runtime, journal, timeout)
        raise RuntimeError('Previous maintenance aborted before changes; old service restored. Retry update explicitly.')
    if journal['phase'] == 'quiescing':
        deadline = time.monotonic() + timeout
        while True:
            receipt = proxy.quiesce(runtime.directory / 'proxy', stack['proxyName'])
            if receipt['status'] == 'drained':
                break
            if time.monotonic() >= deadline:
                return {'status': 'draining', 'scope': 'maintenance', 'message': 'Ingress paused; existing connections retained. Repeat update.'}
            time.sleep(.25)
        journal['phase'] = 'draining'
        save(runtime.journal_file, journal)
    if journal['phase'] == 'draining':
        def persist(worker):
            journal['oldWorker'] = worker
            save(runtime.journal_file, journal)
        try:
            worker = workload_drain.request_worker(journal['oldWorker'], persist)
            worker = workload_drain.wait_worker(worker, timeout)
            persist(worker)
        except Exception as exc:
            journal['drainFailure'] = type(exc).__name__ + ': ' + str(exc)
            restore_before_changes(runtime, journal, timeout)
            raise RuntimeError('Maintenance drain failed before changes; old service restored. ' + str(exc)) from exc
        if worker['status'] != 'drained':
            return {'status': 'draining', 'scope': 'maintenance', 'message': 'Ingress paused; jobs and dependencies retained. Repeat update.'}
        # Inactive services are included explicitly so no old code can survive a migration.
        configured = read(runtime.compose_file)['services']
        services = [name(service, slot) for slot in ('backend', 'backend-green') for service in COHORT
                    if name(service, slot) in configured]
        runtime.dc('stop', *services)
        journal['phase'] = 'preparing'
        save(runtime.journal_file, journal)
    if journal['phase'] == 'preparing':
        base = prepare(journal['targetBase'])
        staged = copy.deepcopy(stack)
        staged.update(active='backend', proxyImage=journal['proxyImage'],
                      images=dict.fromkeys(('backend', 'backend-green'), base['services']['backend']['image']),
                      fleetBases={'backend': base, 'backend-green': copy.deepcopy(base)})
        journal.update(phase='starting', targetBase=base, staged=staged)
        save(runtime.journal_file, journal)
    if journal['phase'] == 'starting':
        # Repeat from the saved plan after interruption, never rebuild or rediscover.
        save(runtime.stack_file, journal['staged'])
        runtime.render(journal['staged'])
        start()
        proxy.docker('update', '--restart=' + journal['targetBase']['services']['worker'].get('restart', 'always'),
                     runtime.container('worker'))
        journal['phase'] = 'opening'
        save(runtime.journal_file, journal)
    if journal['phase'] == 'opening':
        ready(runtime, list(COHORT), timeout)
        proxy.switch(runtime.directory / 'proxy', stack['proxyName'], runtime.container('backend'),
                     sharkd=runtime.container('sharkd'), drain_timeout=0)
        try:
            verify()
        except BaseException:
            proxy.quiesce(runtime.directory / 'proxy', stack['proxyName'])
            raise
        journal['phase'] = 'committing'
        save(runtime.journal_file, journal)
    if journal['phase'] == 'committing':
        commit()
        journal['phase'] = 'committed'
        save(runtime.journal_file, journal)
    save(runtime.directory / 'base.json', journal['targetBase'])
    runtime.journal_file.replace(runtime.directory / f'maintenance-{journal["id"]}.json')
    return {'status': 'ok', 'scope': 'maintenance', 'message': 'Maintenance verified; generation mode retained.'}


def upgrade(layout, args, manifest, *, source, backup_mode, backup_proof, ops):
    """Host adapters retain the existing backup, migration and promotion owners."""
    import subprocess
    from pathlib import Path
    from .fleet_update import pin_release
    from .rolling_update import Runtime, image_ref, fingerprints, activation_base
    runtime = Runtime(layout.state_dir / 'rolling', layout.compose_file, ops._compose_base_command(layout))
    if getattr(args, 'skip_health_check', False):
        raise ValueError('Maintenance requires health verification')
    if (manifest.get('runtimeContract') or {}).get('workerDrainVersion') != 1:
        raise ValueError('Maintenance target must retain generation drain support')
    if runtime.journal_file.exists() and read(runtime.journal_file).get('mode') != 'maintenance':
        raise RuntimeError('Finish or abort the application transaction before maintenance')
    metadata = runtime.directory / 'maintenance-release.json'
    if runtime.journal_file.exists():
        saved = read(metadata)
        if saved['manifest'] != manifest or saved['backupMode'] != backup_mode:
            raise RuntimeError('Resume the saved maintenance release and backup policy')
    else:
        if source == 'manifest' and not getattr(args, 'skip_image_pull', False):
            if ops.deployment_profile(args) == 'saas':
                ops.ensure_ecr_credential_helper_ready(layout)
            for image in sorted({image_ref(value) for value in manifest['images'].values()} - {''}):
                subprocess.run(['docker', 'pull', image], check=True)
        pin_release(runtime, layout.target_release_manifest_path)
        saved = {'manifest': manifest, 'backupMode': backup_mode, 'snapshot': str(ops.snapshot_runtime(layout))}
        save(metadata, saved)
    snapshot = Path(saved['snapshot'])
    def prepare(_):
        if backup_mode == 'inline':
            ops.complete_full_backup(layout, snapshot)
        elif backup_proof:
            ops.record_external_backup_proof(snapshot, backup_proof)
        ops.refresh_managed_sizing_profile(layout)
        ops.render_compose(layout, layout.target_release_manifest_path, profile=ops.deployment_profile(args), maintenance=True)
        ops.render_logging_config(layout)
        return activation_base(runtime.command)
    def start():
        ops.run_target_migrations(layout)
        ops.docker_compose_up(layout, pull_policy='never')
    def verify():
        ops.wait_for_health(timeout_seconds=args.health_timeout)
        ops.wait_for_agent_stream_gateway(layout, timeout_seconds=args.health_timeout)
        ops.wait_for_doctor_ok(args, timeout_seconds=args.health_timeout)
    result = {}
    def commit():
        result.update(ops._promote_release(layout, manifest, snapshot, source=source,
                                         profile=ops.deployment_profile(args), backup_mode=backup_mode))
        stack = read(runtime.stack_file)
        recorded = stack.get('configurationFingerprint', {})
        stack['configurationFingerprint'] = fingerprints([Path(path) for path in recorded])
        save(runtime.stack_file, stack)
    outcome = deploy(runtime, prepare=prepare, start=start, verify=verify, commit=commit,
                     proxy_image=image_ref(manifest['images'].get('deployment-proxy')), timeout=args.health_timeout)
    return {**result, **outcome}
