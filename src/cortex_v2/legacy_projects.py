"""Finite restored-copy reader and project projection; never opens a connection or a file.

Unmapped historical metadata stays in exact SQL JSON bytes in the import ledger.
Native readback covers the fields represented by the project schema independently.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from cortex_v2.state_import import ImportRefused, canonical_json, load_json


@dataclass(frozen=True)
class OriginalRecord:
    source_reference: str
    original_bytes: bytes


def record_digest(records: tuple[OriginalRecord, ...]) -> str:
    digest = hashlib.sha256()
    for record in records:
        for part in (record.source_reference.encode(), record.original_bytes):
            digest.update(len(part).to_bytes(8, 'big'))
            digest.update(part)
    return digest.hexdigest()


@dataclass(frozen=True)
class ProjectSnapshot:
    database: str
    records: tuple[OriginalRecord, ...]
    fingerprint: str
    catalog_sha256: str
    extensions_sha256: str


async def read_project_snapshot(source, *, expected_database: str) -> ProjectSnapshot:
    if not isinstance(expected_database, str) or not re.fullmatch(r'legacy_restore_[a-zA-Z0-9_]+', expected_database):
        raise ImportRefused('source must be an explicitly named restored copy')
    # An existing transaction could defeat the requested snapshot/isolation boundary.
    if source.is_in_transaction():
        raise ImportRefused('source reader requires its own read-only snapshot')
    async with source.transaction(isolation='repeatable_read', readonly=True):
        if await source.fetchval('SELECT current_database()') != expected_database:
            raise ImportRefused('source database mismatch')
        columns = await source.fetch("""
            SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
                   a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_value
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              JOIN pg_attribute a ON a.attrelid=c.oid
              LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
             WHERE n.nspname='public' AND c.relname IN ('cortex_projects','cortex_project_paths')
               AND a.attnum>0 AND NOT a.attisdropped
             ORDER BY c.relname,a.attnum
        """)
        constraints = await source.fetch("""
            SELECT c.relname,k.conname,pg_get_constraintdef(k.oid,true) AS definition
              FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
              JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname IN ('cortex_projects','cortex_project_paths')
             ORDER BY c.relname,k.conname
        """)
        extensions = await source.fetch('SELECT extname,extversion FROM pg_extension ORDER BY extname')
        records = []
        # Fixed identifiers; original bytes come from PostgreSQL, never a Python re-encoding.
        for table, query in (
            ('cortex_projects', 'SELECT id::text AS key,to_jsonb(t)::text AS original FROM public.cortex_projects t ORDER BY id'),
            ('cortex_project_paths', 'SELECT id::text AS key,to_jsonb(t)::text AS original FROM public.cortex_project_paths t ORDER BY id'),
        ):
            for row in await source.fetch(query):
                records.append(OriginalRecord(f'public.{table}:{row["key"]}', row['original'].encode('utf-8')))
        records = tuple(records)
        catalog = canonical_json({'columns': [dict(r) for r in columns], 'constraints': [dict(r) for r in constraints]})
        return ProjectSnapshot(expected_database, records, record_digest(records),
                               hashlib.sha256(catalog.encode()).hexdigest(),
                               hashlib.sha256(canonical_json([dict(r) for r in extensions]).encode()).hexdigest())


@dataclass(frozen=True)
class ProjectGroup:
    records: tuple[OriginalRecord, ...]
    # JSON text keeps the projection immutable across awaits.
    projection_json: str | None
    reason: str | None


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or '\0' in value:
        raise ValueError('invalid text')
    return value


def _time(value) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamp needs offset')
    return parsed.isoformat()


def _projection(project: dict, paths: list[dict], policy: dict) -> dict:
    identity = str(uuid.UUID(project['id']))
    scope = str(uuid.UUID(policy['project_scope_ids'][identity]))
    key = _text(project['project_key'], 96)
    display = _text(project['display_name'], 128)
    status = project['status']
    if status not in ('active', 'archived', 'deleted'):
        raise ValueError('unknown lifecycle')
    repo_root = _text(project['repo_root'], 4096)
    _text(project['repo_type'], 96)  # Historical value is retained, not fabricated into native schema.
    parent = project['parent_project_key']
    if parent is not None:
        _text(parent, 96)
    agent = project['default_agent']
    if agent is not None:
        _text(agent, 128)
    metadata = project['metadata']
    if not isinstance(metadata, dict) or not isinstance(metadata.get('roots'), list):
        raise ValueError('missing_project_roots')
    roots = []
    for path in paths:
        if path['project_key'] != key:
            raise ValueError('path project mismatch')
        uuid.UUID(path['id'])
        _time(path['created_at'])
        details = path['metadata']
        if details is None:
            details = {}
        if not isinstance(details, dict) or {'path', 'kind'} & details.keys():
            raise ValueError('ambiguous path metadata')
        roots.append({'path': _text(path['root_path'], 4096),
                      'kind': _text(path['path_kind'], 96), **details})
    if not roots or len({r['path'] for r in roots}) != len(roots):
        raise ValueError('ambiguous_project_roots')
    primary = [r for r in roots if r['kind'] == 'primary']
    if len(primary) != 1 or primary[0]['path'] != repo_root:
        raise ValueError('ambiguous_project_roots')
    expected = [canonical_json(r) for r in metadata['roots']]
    if len(expected) != len(set(expected)) or sorted(expected) != sorted(canonical_json(r) for r in roots):
        raise ValueError('mismatched_project_roots')
    roots.sort(key=lambda r: (r['kind'] != 'primary', r['path']))
    aliases = metadata.get('aliases', [])
    if not isinstance(aliases, list):
        raise ValueError('ambiguous_project_aliases')
    aliases = [_text(alias, 96) for alias in aliases]
    if len({key, *aliases}) != len(aliases) + 1:
        raise ValueError('ambiguous_project_aliases')
    return dict(scope_id=scope, original_project_id=identity, project_key=key,
                display_name=display, default_agent=agent, status=status,
                parent_project_key=parent, repo_root=repo_root, roots=roots,
                created_at=_time(project['created_at']), updated_at=_time(project['updated_at']),
                aliases=sorted(aliases), is_active=status == 'active')


def project_groups(records: tuple[OriginalRecord, ...], policy: dict) -> tuple[ProjectGroup, ...]:
    projects, paths, seen = [], [], set()
    for record in records:
        if record.source_reference in seen:
            raise ImportRefused('duplicate source reference')
        seen.add(record.source_reference)
        try:
            table, identity = record.source_reference.split(':')
            value = load_json(record.original_bytes)
            if not isinstance(value, dict) or str(uuid.UUID(value['id'])) != identity:
                raise ValueError('source identity mismatch')
            if table == 'public.cortex_projects':
                projects.append((record, value))
            elif table == 'public.cortex_project_paths':
                paths.append((record, value))
            else:
                raise ValueError('unknown source relation')
        except (ValueError, TypeError, KeyError) as exc:
            raise ImportRefused('invalid source record') from exc
    keys = [p['project_key'] for _, p in projects]
    if len(set(keys)) != len(keys):
        raise ImportRefused('ambiguous source project keys')
    groups = []
    for record, project in projects:
        owned = [(r, p) for r, p in paths if p['project_key'] == project['project_key']]
        originals = (record, *(r for r, _ in owned))
        try:
            projection = _projection(project, [p for _, p in owned], policy)
        except (ValueError, TypeError, KeyError) as exc:
            groups.append(ProjectGroup(originals, None, str(exc) or 'invalid_project'))
        else:
            groups.append(ProjectGroup(originals, canonical_json(projection), None))
    for record, path in paths:
        if path['project_key'] not in keys:
            groups.append(ProjectGroup((record,), None, 'orphan_project_path'))
    return tuple(groups)


async def refuse_collisions(target, groups, owned_scopes: set[uuid.UUID]) -> None:
    scopes, aliases = set(), set()
    for group in groups:
        if group.projection_json is None:
            continue
        p = load_json(group.projection_json)
        scope = uuid.UUID(p['scope_id'])
        names = [p['project_key'], *p['aliases']]
        if scope in scopes or aliases.intersection(names):
            raise ImportRefused('ambiguous project mapping')
        scopes.add(scope)
        aliases.update(names)
        if scope in owned_scopes:
            continue
        if await target.fetchval('SELECT EXISTS(SELECT 1 FROM cortex_core.scopes WHERE scope_id=$1)', scope):
            raise ImportRefused('preexisting native scope is not owned by this run')
        if await target.fetchval('SELECT EXISTS(SELECT 1 FROM cortex_core.scope_aliases WHERE alias=ANY($1::text[]))', names):
            raise ImportRefused('preexisting native alias is not owned by this run')
        if await target.fetchval('SELECT EXISTS(SELECT 1 FROM cortex_core.project_registry WHERE original_project_id=$1)', p['original_project_id']):
            raise ImportRefused('preexisting native registry is not owned by this run')


async def write_project(target, projection_json: str, installation: uuid.UUID) -> None:
    p = load_json(projection_json)
    scope, created = uuid.UUID(p['scope_id']), datetime.fromisoformat(p['created_at'])
    await target.execute('INSERT INTO cortex_core.scopes(scope_id,scope_kind,display_name,is_active,created_at) VALUES($1,\'project\',$2,true,$3)', scope, p['display_name'], created)
    await target.execute('INSERT INTO cortex_auth.project_installations(scope_id,installation_id,created_at) VALUES($1,$2,$3)', scope, installation, created)
    await target.execute('''INSERT INTO cortex_core.project_registry
        (project_scope_id,original_project_id,display_name,default_agent,status,parent_project_key,repo_root,roots,created_at,updated_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)''',
        scope, p['original_project_id'], p['display_name'], p['default_agent'], p['status'],
        p['parent_project_key'], p['repo_root'], canonical_json(p['roots']), created,
        datetime.fromisoformat(p['updated_at']))
    for alias in (p['project_key'], *p['aliases']):
        await target.execute('INSERT INTO cortex_core.scope_aliases(alias,scope_id,is_primary,created_at) VALUES($1,$2,$3,$4)', alias, scope, alias == p['project_key'], created)
    if not p['is_active']:
        await target.execute('UPDATE cortex_core.scopes SET is_active=false WHERE scope_id=$1', scope)
    await reconcile_project(target, projection_json, installation)


async def reconcile_project(target, projection_json: str, installation: uuid.UUID) -> None:
    """Read actual native values and compare with a projection rebuilt from source originals."""
    p = load_json(projection_json)
    scope = uuid.UUID(p['scope_id'])
    registry = await target.fetchrow('SELECT * FROM cortex_core.project_registry WHERE project_scope_id=$1 FOR SHARE', scope)
    native_scope = await target.fetchrow('SELECT * FROM cortex_core.scopes WHERE scope_id=$1 FOR SHARE', scope)
    binding = await target.fetchrow('SELECT * FROM cortex_auth.project_installations WHERE scope_id=$1 FOR SHARE', scope)
    aliases = await target.fetch('SELECT alias,is_primary,created_at,retired_at FROM cortex_core.scope_aliases WHERE scope_id=$1 ORDER BY alias FOR SHARE', scope)
    when = datetime.fromisoformat(p['created_at'])
    expected_aliases = {alias: alias == p['project_key'] for alias in (p['project_key'], *p['aliases'])}
    if (registry is None or native_scope is None or binding is None
        or native_scope['scope_kind'] != 'project' or native_scope['display_name'] != p['display_name']
        or native_scope['is_active'] != p['is_active'] or native_scope['created_at'] != when
        or binding['installation_id'] != installation or binding['created_at'] != when
        or {r['alias']: r['is_primary'] for r in aliases} != expected_aliases
        or any(r['retired_at'] is not None or r['created_at'] != when for r in aliases)):
        raise ImportRefused('native project provenance or aliases drifted')
    for field in ('original_project_id', 'display_name', 'default_agent', 'status', 'parent_project_key', 'repo_root'):
        if registry[field] != p[field]:
            raise ImportRefused('native project fields drifted')
    # Python considers True == 1 and False == 0; JSON types must remain distinct.
    if (canonical_json(load_json(registry['roots'])) != canonical_json(p['roots'])
        or registry['created_at'] != when
        or registry['updated_at'] != datetime.fromisoformat(p['updated_at'])):
        raise ImportRefused('native project roots or timestamps drifted')
