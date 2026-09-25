"""Immutable local test images; never publishes images or touches application data."""
import json
from pathlib import Path
import shutil

from packetsafari_onprem import deployment_proxy as proxy
from packetsafari_onprem.schema_upgrade import plan


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
