"""Append-only restored-state import protocol, initially used by the projects family.

The operator supplies connections, fingerprints and approved policy. This module
does not discover credentials, connect to a database, or inspect the filesystem.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass

import asyncpg


class ImportRefused(RuntimeError):
    """Inputs, provenance or native readback do not satisfy the bound conversion."""


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def policy_digest(policy: dict) -> str:
    return hashlib.sha256(canonical_json(policy).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class RunBinding:
    run_id: uuid.UUID
    source_installation_id: uuid.UUID
    source_database: str
    source_snapshot_sha256: str
    source_catalog_sha256: str
    source_extensions_sha256: str
    converter_sha: str
    target_installation_id: uuid.UUID
    target_database: str
    target_schema_sha256: str
    mapping_version: str
    policy_sha256: str


async def native_schema_digest(target) -> str:
    """Fingerprint native definitions and protection, never application row contents.

    No OIDs or literal SQL LIKE patterns enter the fingerprint. Ordering is explicit;
    defaults, checks, indexes, RLS policies, triggers, function bodies and ACLs matter.
    """
    queries = {
        'relations': """SELECT n.nspname,c.relname,c.relkind::text AS relkind,c.relrowsecurity,c.relforcerowsecurity,
                        c.relacl::text AS acl,pg_get_userbyid(c.relowner) AS owner
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname IN ('cortex_core','cortex_auth')
          ORDER BY n.nspname,c.relname""",
        'columns': """SELECT n.nspname,c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
                        a.attnotnull,a.attidentity::text AS attidentity,a.attgenerated::text AS attgenerated,pg_get_expr(d.adbin,d.adrelid) AS default_value
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_attribute a ON a.attrelid=c.oid
          LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
          WHERE n.nspname IN ('cortex_core','cortex_auth') AND a.attnum>0 AND NOT a.attisdropped
          ORDER BY n.nspname,c.relname,a.attnum""",
        'constraints': """SELECT n.nspname,c.relname,k.conname,pg_get_constraintdef(k.oid,true) AS definition
          FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname IN ('cortex_core','cortex_auth') ORDER BY n.nspname,c.relname,k.conname""",
        'indexes': """SELECT n.nspname,c.relname,pg_get_indexdef(c.oid) AS definition
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname IN ('cortex_core','cortex_auth') AND c.relkind='i'
          ORDER BY n.nspname,c.relname""",
        'policies': """SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check
          FROM pg_policies WHERE schemaname IN ('cortex_core','cortex_auth')
          ORDER BY schemaname,tablename,policyname""",
        'triggers': """SELECT n.nspname,c.relname,t.tgname,t.tgenabled::text AS tgenabled,pg_get_triggerdef(t.oid,true) AS definition
          FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname IN ('cortex_core','cortex_auth') AND NOT t.tgisinternal
          ORDER BY n.nspname,c.relname,t.tgname""",
        'functions': """SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) AS arguments,
                        pg_get_functiondef(p.oid) AS definition,p.proacl::text AS acl,
                        pg_get_userbyid(p.proowner) AS owner
          FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname IN ('cortex_core','cortex_auth') AND p.prokind IN ('f','p')
          ORDER BY n.nspname,p.proname,arguments""",
    }
    definitions = {}
    for name, query in queries.items():
        definitions[name] = [dict(r) for r in await target.fetch(query)]
    return hashlib.sha256(canonical_json(definitions).encode('utf-8')).hexdigest()


def _inputs(snapshot, binding: RunBinding, policy: dict) -> tuple[dict, dict]:
    from cortex_v2.legacy_projects import ProjectSnapshot, record_digest

    if not isinstance(binding, RunBinding) or not isinstance(snapshot, ProjectSnapshot):
        raise ImportRefused('typed snapshot and binding required')
    value = asdict(binding)
    for name in ('run_id', 'source_installation_id', 'target_installation_id'):
        if not isinstance(value[name], uuid.UUID):
            raise ImportRefused('binding UUID required')
        value[name] = str(value[name])
    for name in ('source_snapshot_sha256', 'source_catalog_sha256', 'source_extensions_sha256', 'target_schema_sha256', 'policy_sha256'):
        if not isinstance(value[name], str) or not re.fullmatch('[0-9a-f]{64}', value[name]):
            raise ImportRefused('binding SHA256 required')
    if not isinstance(binding.converter_sha, str) or not re.fullmatch('[0-9a-f]{40}', binding.converter_sha):
        raise ImportRefused('converter source SHA required')
    if (not isinstance(binding.source_database, str)
        or not re.fullmatch(r'legacy_restore_[a-zA-Z0-9_]+', binding.source_database)
        or not isinstance(binding.target_database, str) or not binding.target_database
        or binding.mapping_version != 'legacy-projects.v1'):
        raise ImportRefused('unsupported source, target or mapping binding')
    try:
        # Freeze caller-owned policy before the first await.
        approved = json.loads(canonical_json(policy))
        if not isinstance(approved, dict) or approved.get('version') != binding.mapping_version:
            raise ValueError('mapping version')
        mapping = approved['project_scope_ids']
        if not isinstance(mapping, dict):
            raise ValueError('scope map')
        for original, scope in mapping.items():
            if str(uuid.UUID(original)) != original or str(uuid.UUID(scope)) != scope:
                raise ValueError('scope identity')
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ImportRefused('invalid approved project policy') from exc
    if (snapshot.database != binding.source_database
        or snapshot.fingerprint != binding.source_snapshot_sha256
        or record_digest(snapshot.records) != snapshot.fingerprint
        or snapshot.catalog_sha256 != binding.source_catalog_sha256
        or snapshot.extensions_sha256 != binding.source_extensions_sha256
        or policy_digest(approved) != binding.policy_sha256):
        raise ImportRefused('actual source or policy differs from run binding')
    return value, approved


async def _target_guard(target, binding: RunBinding) -> None:
    row = await target.fetchrow('SELECT current_user AS role,current_database() AS database')
    if row['role'] != 'cortex_v2_migrator' or row['database'] != binding.target_database:
        raise ImportRefused('target role or database differs from run binding')
    if await native_schema_digest(target) != binding.target_schema_sha256:
        raise ImportRefused('native schema differs from run binding')
    installation = await target.fetchrow('SELECT status FROM cortex_auth.installations WHERE installation_id=$1 FOR SHARE', binding.target_installation_id)
    if installation is None or installation['status'] != 'active':
        raise ImportRefused('target installation unavailable')


async def _event(target, binding: RunBinding, sequence: int, kind: str, checkpoint: int, payload: dict) -> None:
    await target.execute('''INSERT INTO cortex_core.import_runs
        (run_id,event_seq,event_kind,checkpoint,target_installation_id,payload)
        VALUES($1,$2,$3,$4,$5,$6::jsonb)''', binding.run_id, sequence, kind, checkpoint,
        binding.target_installation_id, canonical_json(payload))


async def _inverse(target, run_id: uuid.UUID) -> dict[str, bytes]:
    from cortex_v2.legacy_projects import OriginalRecord, project_groups, reconcile_project

    header = await target.fetchrow('SELECT payload FROM cortex_core.import_runs WHERE run_id=$1 AND event_seq=0', run_id)
    if header is None:
        raise ImportRefused('unknown import run')
    payload = json.loads(header['payload'])
    rows = await target.fetch('SELECT * FROM cortex_core.import_rows WHERE run_id=$1 ORDER BY ordinal', run_id)
    records = []
    by_reference = {}
    for row in rows:
        original = bytes(row['original_bytes'])
        if hashlib.sha256(original).digest() != row['source_sha256']:
            raise ImportRefused('original row hash mismatch')
        records.append(OriginalRecord(row['source_reference'], original))
        by_reference[row['source_reference']] = row
    installation = uuid.UUID(payload['binding']['target_installation_id'])
    for group in project_groups(tuple(records), payload['policy']):
        stored = [by_reference[r.source_reference] for r in group.records]
        if group.reason is not None:
            if any(r['outcome'] != 'quarantined' or r['reason'] != group.reason for r in stored):
                raise ImportRefused('quarantine disposition differs from originals')
        else:
            scope = uuid.UUID(json.loads(group.projection_json)['scope_id'])
            if any(r['outcome'] != 'migrated' or r['scope_id'] != scope or not json.loads(r['target_references']) for r in stored):
                raise ImportRefused('native disposition differs from originals')
            await reconcile_project(target, group.projection_json, installation)
    return {r.source_reference: r.original_bytes for r in records}


async def read_inverse(target, run_id: uuid.UUID) -> dict[str, bytes]:
    # Independent consistent view; row locks in reconciliation need a read/write transaction,
    # but this operation only issues SELECTs. A caller's transaction retains its own boundary.
    async with target.transaction(isolation='repeatable_read'):
        return await _inverse(target, run_id)


async def import_projects(target, snapshot, binding: RunBinding, policy: dict, *, batch_size: int = 1, fault=None) -> dict:
    """Import finite project groups. Each durable checkpoint includes verified native effects.

    A synchronous fault(stage, checkpoint) is invoked at the two batch crash points.
    Interrupted runs resume the exact committed prefix. Existing unrelated targets refuse;
    this API never upserts or overwrites them, and never issues a credential or grant.
    """
    from cortex_v2.legacy_projects import project_groups, refuse_collisions, write_project

    value, approved = _inputs(snapshot, binding, policy)
    if type(batch_size) is not int or not 1 <= batch_size <= 256:
        raise ImportRefused('batch_size must be between 1 and 256')
    if target.is_in_transaction():
        raise ImportRefused('import engine owns the target transaction boundary')
    groups = project_groups(snapshot.records, approved)
    ordinals = {r.source_reference: n for n, r in enumerate(snapshot.records)}
    originals = {r.source_reference: r.original_bytes for r in snapshot.records}
    lock = int.from_bytes(hashlib.sha256(binding.run_id.bytes).digest()[:8], 'big', signed=True)
    while True:
        committed = None
        receipt = None
        try:
            async with target.transaction():
                # Role check comes before protected relation reads or any target effect.
                role = await target.fetchval('SELECT current_user')
                if role != 'cortex_v2_migrator':
                    raise ImportRefused('import requires the migrator role')
                await target.execute('SELECT pg_advisory_xact_lock($1::bigint)', lock)
                await _target_guard(target, binding)
                events = await target.fetch('SELECT * FROM cortex_core.import_runs WHERE run_id=$1 ORDER BY event_seq', binding.run_id)
                if events:
                    header = json.loads(events[0]['payload'])
                    if header != {'binding': value, 'policy': approved}:
                        raise ImportRefused('immutable run binding changed')
                    checkpoint, sequence = events[-1]['checkpoint'], events[-1]['event_seq']
                    actual = await _inverse(target, binding.run_id)
                    expected = {r.source_reference: r.original_bytes for group in groups[:checkpoint] for r in group.records}
                    if actual != expected or checkpoint > len(groups):
                        raise ImportRefused('checkpoint originals do not match the bound source prefix')
                    if events[-1]['event_kind'] == 'complete':
                        if checkpoint != len(groups) or actual != originals:
                            raise ImportRefused('completed run does not account for every source row')
                        return json.loads(events[-1]['payload'])
                else:
                    checkpoint, sequence = 0, 0
                    await _event(target, binding, 0, 'binding', 0, {'binding': value, 'policy': approved})
                owned = set(await target.fetchval('SELECT coalesce(array_agg(DISTINCT scope_id) FILTER (WHERE scope_id IS NOT NULL),ARRAY[]::uuid[]) FROM cortex_core.import_rows WHERE run_id=$1', binding.run_id))
                # Validate all uncommitted targets before committing the next prefix.
                await refuse_collisions(target, groups, owned)
                next_checkpoint = min(checkpoint + batch_size, len(groups))
                for group in groups[checkpoint:next_checkpoint]:
                    scope, references = None, []
                    if group.projection_json is not None:
                        await write_project(target, group.projection_json, binding.target_installation_id)
                        p = json.loads(group.projection_json)
                        scope = uuid.UUID(p['scope_id'])
                        references = [{'table': table, 'scope_id': str(scope)} for table in (
                            'cortex_core.scopes', 'cortex_auth.project_installations',
                            'cortex_core.scope_aliases', 'cortex_core.project_registry')]
                    for record in group.records:
                        await target.execute('''INSERT INTO cortex_core.import_rows
                            (run_id,ordinal,family,source_reference,original_bytes,source_sha256,outcome,reason,scope_id,target_references)
                            VALUES($1,$2,'projects',$3,$4,$5,$6,$7,$8,$9::jsonb)''',
                            binding.run_id, ordinals[record.source_reference], record.source_reference,
                            record.original_bytes, hashlib.sha256(record.original_bytes).digest(),
                            'quarantined' if group.reason is not None else 'migrated', group.reason,
                            scope, canonical_json(references))
                if next_checkpoint != checkpoint:
                    sequence += 1
                    await _event(target, binding, sequence, 'batch', next_checkpoint, {})
                    committed = next_checkpoint
                actual = await _inverse(target, binding.run_id)
                expected = {r.source_reference: r.original_bytes for group in groups[:next_checkpoint] for r in group.records}
                if actual != expected:
                    raise ImportRefused('native inverse does not reconcile the committed source prefix')
                if next_checkpoint == len(groups):
                    if actual != originals:
                        raise ImportRefused('not every original source row is accounted')
                    counts = Counter(await target.fetchval('SELECT coalesce(array_agg(outcome),ARRAY[]::text[]) FROM cortex_core.import_rows WHERE run_id=$1', binding.run_id))
                    receipt = {'run_id': str(binding.run_id), 'counts': dict(sorted(counts.items())),
                               'checkpoint': next_checkpoint, 'functional_pass': not counts.get('quarantined', 0),
                               'source_snapshot_sha256': binding.source_snapshot_sha256}
                    await _event(target, binding, sequence + 1, 'complete', next_checkpoint, receipt)
                if committed is not None and fault is not None:
                    fault('before_commit', committed)
        except asyncpg.PostgresError as exc:
            # Concurrent different runs can compete for a native unique key. PostgreSQL
            # rolls back this whole batch; no failed row is labelled migrated.
            raise ImportRefused('native import transaction refused') from exc
        if committed is not None and fault is not None:
            fault('after_commit', committed)
        if receipt is not None:
            return receipt


async def read_blob(target, run_id: uuid.UUID, digest: bytes, *, max_bytes: int = 16 * 1024 * 1024) -> bytes:
    """Read a bounded original blob, refusing partial chunks before materializing it.

    Later blob families need a streaming API for larger objects; this finite reader
    deliberately refuses them rather than implying unbounded transfer support.
    """
    if not isinstance(digest, bytes) or len(digest) != 32 or type(max_bytes) is not int or max_bytes < 1:
        raise ImportRefused('invalid blob identity or read bound')
    async with target.transaction(isolation='repeatable_read'):
        meta = await target.fetchrow('''SELECT count(*) AS present,min(total_bytes) AS total,
            max(total_bytes) AS largest,min(chunk_count) AS chunks,max(chunk_count) AS most,
            sum(octet_length(chunk_bytes)) AS bytes
            FROM cortex_core.import_blob_chunks WHERE run_id=$1 AND blob_sha256=$2''', run_id, digest)
        if (not meta['present'] or meta['total'] != meta['largest'] or meta['chunks'] != meta['most']
            or meta['present'] != meta['chunks'] or meta['bytes'] != meta['total'] or meta['total'] > max_bytes):
            raise ImportRefused('incomplete, inconsistent or oversized blob')
        rows = await target.fetch('SELECT chunk_index,chunk_bytes,chunk_sha256 FROM cortex_core.import_blob_chunks WHERE run_id=$1 AND blob_sha256=$2 ORDER BY chunk_index', run_id, digest)
        parts = []
        for index, row in enumerate(rows):
            part = bytes(row['chunk_bytes'])
            if row['chunk_index'] != index or hashlib.sha256(part).digest() != row['chunk_sha256']:
                raise ImportRefused('blob chunk identity mismatch')
            parts.append(part)
        original = b''.join(parts)
        if hashlib.sha256(original).digest() != digest:
            raise ImportRefused('full blob identity mismatch')
        return original
