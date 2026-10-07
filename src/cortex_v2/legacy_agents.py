"""Finite restored-copy identity/profile adapter. Historical authority is data only."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from cortex_v2.legacy_projects import (OriginalRecord, ProjectSnapshot, project_groups,
                                     record_digest, reconcile_project)
from cortex_v2.state_import import ImportRefused, canonical_json, load_json

# Identifiers are fixed source classes, never supplied SQL fragments.
TABLES = ('public.agents', 'public.agent_profiles', 'public.roles',
          'cortex.agent_profiles', 'cortex.roles', 'cortex.role_audit_events')
DEPENDENCIES = ('public.cortex_projects', 'public.cortex_project_paths')
QUERIES = (
    "SELECT id::text AS key,to_jsonb(t)::text AS original FROM public.agents t ORDER BY id",
    "SELECT id::text AS key,to_jsonb(t)::text AS original FROM public.agent_profiles t ORDER BY id",
    "SELECT jsonb_build_array(project,name)::text AS key,to_jsonb(t)::text AS original FROM public.roles t ORDER BY project,name",
    "SELECT id::text AS key,to_jsonb(t)::text AS original FROM cortex.agent_profiles t ORDER BY id",
    "SELECT id::text AS key,to_jsonb(t)::text AS original FROM cortex.roles t ORDER BY id",
    "SELECT id::text AS key,to_jsonb(t)::text AS original FROM cortex.role_audit_events t ORDER BY created_at,id",
    "SELECT id::text AS key,to_jsonb(t)::text AS original FROM public.cortex_projects t ORDER BY id",
    "SELECT id::text AS key,to_jsonb(t)::text AS original FROM public.cortex_project_paths t ORDER BY id",
)


@dataclass(frozen=True)
class AgentSnapshot(ProjectSnapshot):
    dependencies: tuple[OriginalRecord, ...]

    @property
    def fingerprint_records(self):
        return self.records + self.dependencies


async def read_agent_snapshot(source, *, expected_database: str) -> AgentSnapshot:
    if not isinstance(expected_database, str) or not re.fullmatch(r'legacy_restore_[a-zA-Z0-9_]+', expected_database):
        raise ImportRefused('source must be an explicitly named restored copy')
    if source.is_in_transaction():
        raise ImportRefused('source reader requires its own read-only snapshot')
    async with source.transaction(isolation='repeatable_read', readonly=True):
        if await source.fetchval('SELECT current_database()') != expected_database:
            raise ImportRefused('source database mismatch')
        names = [*TABLES, *DEPENDENCIES]
        present = {name: await source.fetchval('SELECT to_regclass($1)::text', name) is not None for name in names}
        if not all(present[name] for name in ('public.agents', *DEPENDENCIES)):
            raise ImportRefused('required source identity or project relation missing')
        columns = await source.fetch("""SELECT n.nspname,c.relname,a.attname,
            format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull,
            pg_get_expr(d.adbin,d.adrelid) AS default_value
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid
            LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
            WHERE n.nspname || '.' || c.relname=ANY($1::text[]) AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY n.nspname,c.relname,a.attnum""", names)
        constraints = await source.fetch("""SELECT n.nspname,c.relname,k.conname,
            pg_get_constraintdef(k.oid,true) AS definition FROM pg_constraint k
            JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname || '.' || c.relname=ANY($1::text[]) ORDER BY n.nspname,c.relname,k.conname""", names)
        extensions = await source.fetch('SELECT extname,extversion FROM pg_extension ORDER BY extname')
        records, dependencies = [], []
        for name, query in zip(names, QUERIES):
            if present[name]:
                destination = records if name in TABLES else dependencies
                for row in await source.fetch(query):
                    destination.append(OriginalRecord(f'{name}:{row["key"]}', row['original'].encode('utf-8')))
        records, dependencies = tuple(records), tuple(dependencies)
        catalog = {'present': present, 'columns': [dict(r) for r in columns], 'constraints': [dict(r) for r in constraints]}
        return AgentSnapshot(expected_database, records, record_digest(records + dependencies),
                             hashlib.sha256(canonical_json(catalog).encode()).hexdigest(),
                             hashlib.sha256(canonical_json([dict(r) for r in extensions]).encode()).hexdigest(), dependencies)


def context_records(context):
    try:
        return tuple(OriginalRecord(r['source_reference'], r['original'].encode('utf-8')) for r in context)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ImportRefused('invalid bound project context') from exc


def _committed_agent_groups(records, policy, context, checkpoint):
    # Originals cover only committed groups. A future nonempty quarantine must
    # not be reconstructed as an invalid empty family by either native reader.
    contexts = project_groups(context_records(context), policy)[:checkpoint]
    dependencies = tuple(record for group in contexts for record in group.records)
    return agent_groups(records, policy, dependencies)[:checkpoint]


@dataclass(frozen=True)
class AgentGroup:
    records: tuple[OriginalRecord, ...]
    projection_json: str | None
    reason: str | None
    context_json: str | None


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('invalid explicit UUID')
    return value


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or '\0' in value:
        raise ValueError('invalid explicit owner or identity name')
    return value


def _item(record, value, table, project, policy):
    mapping = policy['record_mappings'][record.source_reference]
    if mapping['source_project_id'] != project['original_project_id']:
        raise ValueError('conflicting project mapping')
    if value.get('project_id') is not None and value['project_id'] != mapping['source_project_id']:
        raise ValueError('conflicting original project reference')
    if table.startswith('public.') and value.get('project') not in (project['project_key'], '_global'):
        raise ValueError('unknown original project key')
    if table.startswith('cortex.'):
        customer = _uuid(value['customer_id'])
        if customer != mapping['customer_id'] or customer != policy['project_customer_ids'][project['original_project_id']]:
            raise ValueError('conflicting customer mapping')
    lineage = {key: _uuid(mapping[key]) for key in ('actor_id', 'principal_id')}
    if mapping['authority'] != 'dormant':
        raise ValueError('historical authority must stay dormant')
    lineage['authority'] = 'dormant'
    for source_field, mapped_field in (('actor_id', 'source_actor_id'), ('actor_user_id', 'source_author_user_id')):
        if value.get(source_field) is not None:
            if _uuid(value[source_field]) != _uuid(mapping[mapped_field]):
                raise ValueError('conflicting historical author mapping')
            lineage[mapped_field] = value[source_field]
    name = _name(mapping['agent_name'])
    if table in ('public.agents', 'cortex.agent_profiles'):
        if name != value['name']:
            raise ValueError('invented agent identity name')
        identity, profile = _uuid(mapping['identity_id']), None
    else:
        identity = _uuid(mapping['identity_id']) if 'identity_id' in mapping else None
        profile = _uuid(mapping['profile_id'])
        if table == 'public.agent_profiles' and name != value['agent_name']:
            raise ValueError('conflicting profile owner')
        if identity is not None and table != 'public.agent_profiles':
            raise ValueError('only an explicitly mapped agent profile may supply a profile-only identity')
    return {'source_reference': record.source_reference, 'source_class': table,
            'identity_id': identity, 'profile_id': profile, 'agent_name': name,
            'original_record': value, 'lineage': lineage}


def agent_groups(records, policy, dependencies):
    contexts = project_groups(dependencies, policy)
    if any(g.reason is not None for g in contexts):
        raise ImportRefused('project dependency cannot be projected')
    projects = [load_json(g.projection_json) for g in contexts]
    by_id = {p['original_project_id']: p for p in projects}
    by_key = {p['project_key']: p['original_project_id'] for p in projects}
    grouped = {pid: [] for pid in by_id}
    orphans, seen = [], set()
    for record in records:
        if record.source_reference in seen:
            raise ImportRefused('duplicate source reference')
        seen.add(record.source_reference)
        try:
            table, key = record.source_reference.split(':', 1)
            value = load_json(record.original_bytes)
            if table not in TABLES or not isinstance(value, dict):
                raise ValueError('unknown source class')
            if table == 'public.roles':
                if canonical_json(load_json(key)) != canonical_json([value['project'], value['name']]):
                    raise ValueError('composite source reference mismatch')
            elif _uuid(value['id']) != key:
                raise ValueError('source identity mismatch')
        except (KeyError, TypeError, ValueError) as exc:
            raise ImportRefused('invalid source record') from exc
        pid = value.get('project_id') or by_key.get(value.get('project'))
        if pid is None and value.get('project') == '_global':
            pid = policy.get('record_mappings', {}).get(record.source_reference, {}).get('source_project_id')
        if pid in grouped:
            grouped[pid].append((record, value, table))
        else:
            orphans.append(AgentGroup((record,), None, 'orphan_agent_scope', None))
    groups = []
    originals = {load_json(r.original_bytes)['id']: load_json(r.original_bytes) for r in dependencies if r.source_reference.startswith('public.cortex_projects:')}
    for project in projects:
        pid = project['original_project_id']; owned = grouped[pid]
        original_records = tuple(r for r, _, _ in owned)
        context_json = canonical_json(project)
        try:
            items = [_item(r, value, table, project, policy) for r, value, table in owned]
            owners = {item['agent_name'] for item in items if item['identity_id'] is not None}
            if any(item['source_class'] == 'public.agent_profiles' and item['agent_name'] not in owners for item in items):
                raise ValueError('orphan public agent profile owner')
            role_names = {v['name'] for _, v, t in owned if t in ('public.roles', 'cortex.roles')}
            if any(v['role_name'] not in role_names for _, v, t in owned if t == 'cortex.role_audit_events'):
                raise ValueError('orphan role history')
            roster = None
            original = originals[pid]
            if 'roster_policy' in original['metadata']:
                approved = policy['roster_profiles'][pid]
                roster = {'profile_id': _uuid(approved['profile_id']), 'agent_name': _name(approved['agent_name']),
                          'source_reference': 'roster:public.cortex_projects:' + pid, 'original_record': original}
            projection = {'scope_id': project['scope_id'], 'project': project, 'items': items, 'roster': roster}
            groups.append(AgentGroup(original_records, canonical_json(projection), None, context_json))
        except (KeyError, TypeError, ValueError) as exc:
            if not original_records:
                raise ImportRefused('empty agent family has an invalid project mapping') from exc
            groups.append(AgentGroup(original_records, None, str(exc) or 'invalid_agent_mapping', context_json))
    return tuple(groups + sorted(orphans, key=lambda g: g.records[0].source_reference))


def _targets(projection):
    for item in projection['items']:
        for kind in ('identity', 'profile'):
            if item[kind + '_id'] is not None:
                yield kind, item[kind + '_id'], item['source_reference'], item['agent_name'], item['original_record']
    if projection['roster'] is not None:
        item = projection['roster']
        yield 'profile', item['profile_id'], item['source_reference'], item['agent_name'], item['original_record']


async def refuse_agent_collisions(target, groups, checkpoint, installation):
    ids, names, references = set(), set(), set()
    for index, group in enumerate(groups):
        if group.context_json is not None:
            await reconcile_project(target, group.context_json, installation)
        if group.projection_json is None:
            continue
        p = load_json(group.projection_json); scope = uuid.UUID(p['scope_id'])
        for kind, identity, reference, name, _ in _targets(p):
            key = (kind, identity); natural = (kind, p['scope_id'], reference)
            named = (p['scope_id'], name)
            if key in ids or natural in references or (kind == 'identity' and named in names):
                raise ImportRefused('ambiguous identity/profile mapping')
            ids.add(key); references.add(natural)
            if kind == 'identity':
                names.add(named)
            if index < checkpoint:
                continue
            # Fixed target relations/columns, with values passed as parameters.
            if kind == 'identity':
                exists = await target.fetchval('''SELECT EXISTS(SELECT 1 FROM cortex_core.project_identities
                    WHERE identity_id=$1 OR (project_scope_id=$2 AND (original_identity_id=$3 OR identity_name=$4)))''', uuid.UUID(identity), scope, reference, name)
            else:
                exists = await target.fetchval('''SELECT EXISTS(SELECT 1 FROM cortex_core.project_profiles
                    WHERE profile_id=$1 OR (project_scope_id=$2 AND original_profile_id=$3))''', uuid.UUID(identity), scope, reference)
            if exists:
                raise ImportRefused('unowned native identity/profile collision')


async def write_agents(target, projection_json, installation):
    p = load_json(projection_json)
    await reconcile_project(target, canonical_json(p['project']), installation)
    scope = uuid.UUID(p['scope_id'])
    for kind, identity, reference, name, original in _targets(p):
        if kind == 'identity':
            await target.execute("INSERT INTO cortex_core.project_identities VALUES($1,$2,$3,$4,'agent',$5::jsonb)", uuid.UUID(identity), scope, reference, name, canonical_json(original))
        else:
            await target.execute('INSERT INTO cortex_core.project_profiles VALUES($1,$2,$3,$4,$5::jsonb)', uuid.UUID(identity), scope, reference, name, canonical_json(original))
    await reconcile_agents(target, projection_json, installation)


async def reconcile_agents(target, projection_json, installation):
    p = load_json(projection_json)
    await reconcile_project(target, canonical_json(p['project']), installation)
    scope = uuid.UUID(p['scope_id'])
    for kind, identity, reference, name, original in _targets(p):
        if kind == 'identity':
            row = await target.fetchrow('SELECT project_scope_id,original_identity_id AS reference,identity_name AS name,identity_kind,original_record FROM cortex_core.project_identities WHERE identity_id=$1 FOR SHARE', uuid.UUID(identity))
        else:
            row = await target.fetchrow('SELECT project_scope_id,original_profile_id AS reference,agent_name AS name,original_record FROM cortex_core.project_profiles WHERE profile_id=$1 FOR SHARE', uuid.UUID(identity))
        if (row is None or row['project_scope_id'] != scope or row['reference'] != reference or row['name'] != name
            or (kind == 'identity' and row['identity_kind'] != 'agent')
            or canonical_json(load_json(row['original_record'])) != canonical_json(original)):
            raise ImportRefused('native identity/profile readback drifted')


def target_references(projection_json, reference):
    p = load_json(projection_json)
    return [{'table': 'cortex_core.project_' + ('identities' if kind == 'identity' else 'profiles'),
             'id': identity, 'scope_id': p['scope_id'], 'source_reference': original_reference}
            for kind, identity, original_reference, _, _ in _targets(p) if original_reference == reference]


async def read_native_roster(target, run_id):
    """Read verified native rows into explicitly classified finite roster views."""
    from cortex_v2.state_import import _inverse
    async with target.transaction(isolation='repeatable_read'):
        originals = await _inverse(target, run_id)
        header = load_json(await target.fetchval('SELECT payload FROM cortex_core.import_runs WHERE run_id=$1 AND event_seq=0', run_id))
        if header['binding']['mapping_version'] != 'legacy-agents.v1':
            raise ImportRefused('native roster reader requires the agents family')
        records = tuple(OriginalRecord(key, value) for key, value in originals.items())
        checkpoint = await target.fetchval('SELECT checkpoint FROM cortex_core.import_runs WHERE run_id=$1 ORDER BY event_seq DESC LIMIT 1', run_id)
        result = []
        for group in _committed_agent_groups(records, header['policy'], header['context'], checkpoint):
            if group.projection_json is None:
                continue
            p = load_json(group.projection_json); actual = []
            for item in p['items']:
                if item['identity_id'] is not None:
                    raw = await target.fetchval('SELECT original_record FROM cortex_core.project_identities WHERE identity_id=$1', uuid.UUID(item['identity_id']))
                else:
                    raw = await target.fetchval('SELECT original_record FROM cortex_core.project_profiles WHERE profile_id=$1', uuid.UUID(item['profile_id']))
                actual.append({**item, 'original_record': load_json(raw)})
            roster = {}
            if p['roster'] is not None:
                raw = await target.fetchval('SELECT original_record FROM cortex_core.project_profiles WHERE profile_id=$1', uuid.UUID(p['roster']['profile_id']))
                roster = load_json(raw)['metadata']['roster_policy']
            history = [r for r in actual if r['source_class'] == 'cortex.role_audit_events']
            history.sort(key=lambda r: (datetime.fromisoformat(r['original_record']['created_at']), r['original_record']['id']))
            result.append({'source_project_id': p['project']['original_project_id'], 'scope_id': p['scope_id'],
                           'roster_policy': roster, 'records': actual,
                           'roles': [r for r in actual if r['source_class'] in ('public.roles', 'cortex.roles')],
                           'profiles': [r for r in actual if r['profile_id'] is not None], 'history': history})
        return result
