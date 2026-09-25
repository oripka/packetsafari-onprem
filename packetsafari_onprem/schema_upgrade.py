"""Fail-closed compatibility decisions over signed, image-derived schema inputs."""
import re


def plan(old, new):
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ValueError('Release runtime/schema contracts are missing')
    keys = set(old) | set(new)
    if (new.get('protocolVersion') != 1 or new.get('workerDrainVersion') != 1 or
            any(old.get(k) != new.get(k) for k in keys - {'schemaInputs', 'schemaMigrations'})):
        raise ValueError('Release runtime contracts differ; maintenance required')
    pair = [old.get('schemaInputs'), new.get('schemaInputs')]
    if not all(re.fullmatch('[a-f0-9]{64}', str(value)) for value in pair):
        raise ValueError('Release schema identity is invalid')
    if pair[0] == pair[1]:
        if old.get('schemaMigrations') and old['schemaMigrations'] != new.get('schemaMigrations'):
            raise ValueError('Migration metadata/environment changed; maintenance required')
        return {'inputs': pair, 'migrations': []}
    before, after = old.get('schemaMigrations') or {}, new.get('schemaMigrations') or {}
    if (before.get('format') != 1 or after.get('format') != 1 or
            not before.get('environment') or before['environment'] != after.get('environment')):
        raise ValueError('Schema migration environment changed or legacy metadata is missing; maintenance required')

    def graph(value):
        rows = value.get('files') or []
        result = {}
        for row in rows:
            revision = row.get('revision')
            if (not isinstance(revision, str) or not re.fullmatch(r'[a-zA-Z0-9_]+', revision) or
                    revision in result or not re.fullmatch('[a-f0-9]{64}', str(row.get('sha256'))) or
                    not isinstance(row.get('parent'), (str, list, type(None)))):
                raise ValueError('Invalid or branched migration history; maintenance required')
            result[revision] = row
        def parents(row):
            value = row['parent']
            return value if isinstance(value, list) else ([value] if value is not None else [])
        if any(not isinstance(p, str) for row in rows for p in parents(row)):
            raise ValueError('Invalid migration parents')
        heads = set(result) - {p for row in rows for p in parents(row)}
        if len(heads) != 1:
            raise ValueError('Online upgrades require a single migration head')
        head = next(iter(heads))
        seen, visiting = set(), set()
        def visit(cursor):
            if cursor not in result or cursor in visiting:
                raise ValueError('Incomplete or cyclic migration history')
            if cursor in seen:
                return
            visiting.add(cursor)
            for parent in parents(result[cursor]):
                visit(parent)
            visiting.remove(cursor)
            seen.add(cursor)
        visit(head)
        if seen != set(result):
            raise ValueError('Disconnected migration history')
        return result, head

    previous, source = graph(before)
    following, target = graph(after)
    if any(following.get(key) != row for key, row in previous.items()):
        raise ValueError('Existing migrations were changed or removed; maintenance required')
    added, cursor = [], target
    while cursor != source:
        row = following.get(cursor)
        if (row is None or cursor in previous or row.get('rollingCompatible') is not True or
                not isinstance(row['parent'], str)):
            raise ValueError('New migrations lack rolling_compatible review; maintenance required')
        added.append(cursor)
        cursor = row['parent']
    if set(added) != set(following) - set(previous):
        raise ValueError('Online migration history is not an append-only chain')
    return {'inputs': pair, 'fromRevision': source, 'targetRevision': target,
            'migrations': list(reversed(added))}
