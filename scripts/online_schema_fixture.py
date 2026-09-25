"""Immutable local test images; never publishes images or touches application data."""
import json
from pathlib import Path
import shutil
import copy
import time

from packetsafari_onprem import deployment_proxy as proxy
from packetsafari_onprem.schema_upgrade import plan
from packetsafari_onprem import fleet_update as fleet
from packetsafari_onprem.rolling_update import read


def prepare_images(root, source_image):
    context = root / 'image-context'
    context.mkdir()
    backend = Path(__file__).resolve().parents[2] / 'packetsafari/backend'
    files = ['scripts/deployment_contract.py', 'scripts/sql_storage_upgrade.py',
             'scripts/worker_supervisor.sh', 'packetsafari/storage/sql/migrations.py', 'alembic/env.py']
    for relative in files:
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backend / relative, target)
    dockerfile = context / 'Dockerfile'
    dockerfile.write_text('FROM ' + source_image + '\n' + '\n'.join(
        f'COPY {relative} /app/{relative}' for relative in files) + '\n')
    images, contracts = {}, {}
    for slot in ('blue', 'green'):
        tag = root.name + ':' + slot
        proxy.docker('build', '-q', '-t', tag, str(context))
        images[slot] = proxy.inspect(tag)['Id']
        contracts[slot] = json.loads(proxy.docker('run', '--rm', '--network', 'none', '--entrypoint',
            'python3', images[slot], '/app/scripts/deployment_contract.py').stdout)
        if slot == 'blue':
            rows = contracts[slot]['schemaMigrations']['files']
            parents = {p for row in rows for p in (row['parent'] if isinstance(row['parent'], list) else [row['parent']])}
            heads = {row['revision'] for row in rows} - parents
            assert len(heads) == 1
            (context / 'fixture_migration.py').write_text(
                "from alembic import op\nrevision='online_fixture_000077'\n" +
                f"down_revision={next(iter(heads))!r}\nrolling_compatible=True\n" +
                "def upgrade():\n    op.execute('CREATE TABLE online_schema_fixture (id integer)')\n")
            dockerfile.write_text('FROM ' + tag + '\nCOPY fixture_migration.py /app/alembic/versions/fixture_migration.py\n')
    return images, plan(contracts['blue'], contracts['green'])


def test_followup_updates(root, runtime, current_base, samples, events):
    """Exercise failure/abort plus two further migrations, alternating slots."""
    def contract(image):
        return json.loads(proxy.docker('run', '--rm', '--network', 'none', '--entrypoint',
            'python3', image, '/app/scripts/deployment_contract.py').stdout)
    def sql(query):
        return proxy.docker('exec', runtime.container('postgres'), 'psql', '-U', 'postgres', '-Atc', query).stdout.strip()
    for number, statement in enumerate([
            'ALTER TABLE online_schema_fixture ADD COLUMN note text',
            'DROP TABLE online_schema_fixture'], start=78):
        parent_image = current_base['services']['backend']['image']
        before = contract(parent_image)
        parent_revision = sql('SELECT version_num FROM alembic_version')
        for fail in ([True, False] if number == 78 else [False]):
            context = root / f'followup-{number}-{fail}'
            context.mkdir()
            tag = root.name + f':followup-{number}-{fail}'.lower()
            parent_tag = root.name + ':parent'
            proxy.docker('tag', parent_image, parent_tag)
            revision = f'online_fixture_0000{number}'
            (context/'migration.py').write_text(
                f"from alembic import op\nrevision={revision!r}\ndown_revision={parent_revision!r}\nrolling_compatible=True\n" +
                f"def upgrade():\n    op.execute({statement!r})\n" +
                ("    op.execute('SELECT 1/0')\n" if fail else ''))
            (context/'Dockerfile').write_text(f'FROM {parent_tag}\nCOPY migration.py /app/alembic/versions/followup_{number}.py\n')
            proxy.docker('build', '-q', '-t', tag, str(context))
            image = proxy.inspect(tag)['Id']
            after = contract(image)
            schema = plan(before, after)
            active = read(runtime.stack_file)['active']
            candidate = 'backend' if active == 'backend-green' else 'backend-green'
            target = copy.deepcopy(current_base)
            for service in fleet.COHORT:
                target['services'][service]['image'] = image
                target['services'][service]['environment']['SLOT'] = candidate
            start, sample_start = time.monotonic(), len(samples)
            if fail:
                try:
                    fleet.deploy(runtime, target, schema=schema, timeout=60)
                    raise AssertionError('Injected SQL failure accepted')
                except Exception:
                    assert read(runtime.journal_file)['phase'] == 'migrating'
                    assert sql('SELECT version_num FROM alembic_version') == parent_revision
                    assert sql("SELECT count(*) FROM information_schema.columns WHERE table_name='online_schema_fixture' AND column_name='note'") == '0'
                    assert read(runtime.stack_file)['active'] == active
                    assert fleet.abort(runtime)['status'] == 'rolled_back'
                events.append({'check': 'failed transactional DDL preserved old traffic/schema; abort cleared saved attempt'})
            else:
                result = fleet.deploy(runtime, target, schema=schema, timeout=60)
                assert result['status'] == 'ok', result
                assert read(runtime.stack_file)['active'] == candidate
                assert sql('SELECT version_num FROM alembic_version') == revision
                current_base = target
                events.append({'check': 'compatible followup without maintenance', 'revision': revision,
                    'active': candidate, 'totalSeconds': round(time.monotonic()-start, 3),
                    'httpSamples': len(samples)-sample_start, 'maxRequestSeconds': max(s['seconds'] for s in samples[sample_start:])})
            assert not any(s['error'] for s in samples[sample_start:])
    assert sql("SELECT to_regclass('online_schema_fixture') IS NULL") == 't'
