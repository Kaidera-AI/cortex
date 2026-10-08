"""Finite canonical-family adapter for explicitly restored legacy snapshots.

No connection, filesystem, authority or binding discovery. The operator supplies
connections and approved mappings. Whole bounded batches and original records
share an atomic command receipt; this is not the all-class checkpoint importer.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import PurePosixPath
import re
import uuid
import asyncpg
from .legacy_projects import OriginalRecord
from .receipts import begin_command, commit_receipt, request_digest
from .state_import import ImportRefused
from .store import ApiProblem

TABLES = {'public.agent_profiles':'persona','public.rules':'rule','public.agent_skills':'skill'}
OPERATION = 'legacy.boot.import'
LIMIT = 1048576


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _uuid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value: raise ValueError
    return uuid.UUID(value)


def _path(value):
    if (not isinstance(value,str) or not value or value.startswith('/') or '\\' in value
            or any(ord(c)<32 or ord(c)==127 for c in value)
            or any(p in ('','.','..') for p in value.split('/'))
            or str(PurePosixPath(value))!=value): raise ValueError
    return value


@dataclass(frozen=True)
class BootSnapshot:
    database: str
    records: tuple[OriginalRecord,...]
    bodies: tuple[tuple[str,str],...]
    fingerprint: str


def _fingerprint(database,records,bodies):
    payload={'database':database,'rows':[{'reference':r.source_reference,'sha256':hashlib.sha256(r.original_bytes).hexdigest()} for r in records],
             'bodies':[{'reference':ref,'sha256':hashlib.sha256(body.encode()).hexdigest()} for ref,body in bodies]}
    return hashlib.sha256(_json(payload).encode()).hexdigest()


async def read_snapshot(source,*,expected_database,bodies):
    try:
        if not isinstance(bodies,dict) or len(bodies)>64: raise ValueError
        frozen=tuple(sorted(bodies.items()))
        for ref,body in frozen:
            _path(ref)
            if not isinstance(body,str) or not 1<=len(body.encode())<=65536 or '\0' in body: raise ValueError
        records=[]
        async with source.transaction(isolation='repeatable_read',readonly=True):
            database=await source.fetchval('SELECT current_database()')
            if database!=expected_database: raise ValueError
            for table in TABLES:
                for row in await source.fetch(f'SELECT id,to_jsonb(t)::text AS original FROM {table} t ORDER BY id'):
                    reference=table+':'+str(row['id']);_uuid(str(row['id']))
                    original=row['original'].encode('utf-8')
                    records.append(OriginalRecord(reference,original))
        records=tuple(sorted(records,key=lambda r:r.source_reference))
        if not 1<=len(records)<=64 or len({r.source_reference for r in records})!=len(records): raise ValueError
        if sum(len(r.original_bytes) for r in records)+sum(len(v.encode()) for _,v in frozen)>LIMIT: raise ValueError
        return BootSnapshot(database,records,frozen,_fingerprint(database,records,frozen))
    except Exception:
        raise ImportRefused('legacy_boot_snapshot_refused') from None


def _inputs(snapshot,policy,key):
    try:
        if not isinstance(snapshot,BootSnapshot) or snapshot.fingerprint!=_fingerprint(snapshot.database,snapshot.records,snapshot.bodies): raise ValueError
        if not 1<=len(snapshot.records)<=64 or len({r.source_reference for r in snapshot.records})!=len(snapshot.records): raise ValueError
        if sum(len(r.original_bytes) for r in snapshot.records)+sum(len(v.encode()) for _,v in snapshot.bodies)>LIMIT: raise ValueError
        approved=json.loads(_json(policy))
        if set(approved)!={'schema','source_database','source_snapshot_sha256','target_installation_id','target_database','principal_id','projects','records'}: raise ValueError
        if approved['schema']!='cortex.legacy-boot-policy.v1' or approved['source_database']!=snapshot.database or approved['source_snapshot_sha256']!=snapshot.fingerprint: raise ValueError
        _uuid(approved['principal_id']);_uuid(approved['target_installation_id'])
        if not isinstance(approved['target_database'],str) or not 1<=len(approved['target_database'])<=63 or any(ord(c)<32 for c in approved['target_database']): raise ValueError
        if not isinstance(key,str) or not 1<=len(key)<=128 or any(ord(c)<32 for c in key): raise ValueError
        projects=approved['projects']
        if not isinstance(projects,dict) or not projects or len(projects)>64: raise ValueError
        for name,mapping in projects.items():
            if not isinstance(name,str) or not name or set(mapping)!={'scope_id','scope'} or mapping['scope'] not in ('project','global'): raise ValueError
            _uuid(mapping['scope_id'])
        if not isinstance(approved['records'],dict) or set(approved['records'])!={r.source_reference for r in snapshot.records}: raise ValueError
        identities=set()
        for record in snapshot.records:
            table,identity=record.source_reference.split(':');_uuid(identity)
            original=json.loads(record.original_bytes)
            if table not in TABLES or original['id']!=identity: raise ValueError
            mapping=approved['records'][record.source_reference]
            if (set(mapping)!={'kind','id','revision','audience','obligation'} or mapping['kind']!=TABLES[table]
                    or mapping['id']!=identity or type(mapping['revision']) is not int or not 1<=mapping['revision']<=2147483647
                    or mapping['audience'] not in ('scope','agent_boot') or mapping['obligation'] not in ('mandatory','optional')): raise ValueError
            target=(mapping['kind'],mapping['id'],mapping['revision'])
            if target in identities: raise ValueError
            identities.add(target)
        if len(_json(approved).encode())>65536: raise ValueError
        return approved
    except Exception:
        raise ImportRefused('legacy_boot_policy_refused') from None


def _project(record,mapping,projects,bodies,principal):
    row=json.loads(record.original_bytes);kind=mapping['kind'];project=projects.get(row.get('project'))
    item={'source_reference':record.source_reference,'original':record.original_bytes.decode(),'source_sha256':hashlib.sha256(record.original_bytes).hexdigest(),
          'kind':kind,'outcome':'quarantined','reason':None,'native':None,'body_sha256':None}
    def quarantine(reason): item['reason']=reason;return item
    if project is None: return quarantine('unmapped_project')
    if kind=='persona' and row.get('profile_kind')!='identity': return quarantine('unsupported_profile_kind')
    if kind in ('rule','skill') and row.get('status') not in (('active','deprecated') if kind=='rule' else ('active',)): return quarantine('unsupported_status')
    metadata=row.get('metadata')
    if metadata is None: metadata={}
    if not isinstance(metadata,dict): return quarantine('invalid_metadata')
    source_scope=row.get('scope') if kind=='skill' else metadata.get('scope',project['scope'])
    if source_scope!=project['scope']: return quarantine('scope_mapping_mismatch')
    body=row.get('profile_text') if kind=='persona' else row.get('body') if kind=='rule' else bodies.get(row.get('body_ref'))
    if body is None: return quarantine('body_missing')
    if not isinstance(body,str) or not 1<=len(body.encode())<=65536 or '\0' in body: return quarantine('invalid_body')
    body_hash=hashlib.sha256(body.encode()).hexdigest()
    if kind=='skill' and row.get('body_hash')!=body_hash: return quarantine('body_hash_mismatch')
    manifest={'schema_version':f'cortex.boot-{kind}-manifest.v1','name':row.get('agent_name') if kind=='persona' else row.get('name'),
              'description':metadata.get('description') if kind!='skill' else row.get('description'), 'scope':project['scope'],
              'permission':metadata.get('permission') if kind!='skill' else row.get('permission'),
              'body_ref':row.get('source_file') if kind!='skill' else row.get('body_ref'),
              'version':metadata.get('version') if kind=='persona' else row.get('version')}
    try:
        if manifest['body_ref'] is not None: _path(manifest['body_ref'])
        for field in ('name','description','permission','version'):
            if manifest[field] is not None and not isinstance(manifest[field],str): raise ValueError
        timestamp=datetime.fromisoformat(row['updated_at' if kind=='persona' else 'created_at'])
        if timestamp.tzinfo is None: raise ValueError
        native={'scope_id':project['scope_id'],kind+'_id':mapping['id'],'revision':mapping['revision'],'body':body,
                'created_by_principal':principal,'created_at':timestamp.isoformat(),'boot_manifest':manifest}
        if kind=='persona':
            roles=metadata.get('functional_roles',[row.get('role')])
            if (not isinstance(roles,list) or not 1<=len(roles)<=64 or any(not isinstance(r,str) or not 1<=len(r)<=256 for r in roles) or len(set(roles))!=len(roles)): raise ValueError
            manifest.update(functional_roles=roles,identity_text=body)
            native.update(template_version='cortex.persona.v2',payload={'source_reference':record.source_reference,'original_record':row},audience=mapping['audience'])
        else:
            slug=row['rule_slug' if kind=='rule' else 'skill_slug']
            if not isinstance(slug,str) or re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,95}',slug) is None: raise ValueError
            native['slug']=slug
            if kind=='rule':
                manifest.update(title=row['title'],source_file=row.get('source_file'))
                if not isinstance(row['title'],str): raise ValueError
                native.update(obligation=mapping['obligation'],state='active' if row['status']=='active' else 'retired',audience=mapping['audience'])
            else:
                when=metadata.get('when_to_use')
                if not isinstance(when,str) or not 1<=len(when)<=2048: raise ValueError
                native['when_to_use']=when
    except (ValueError,TypeError,KeyError): return quarantine('invalid_canonical_metadata')
    item.update(outcome='migrated',reason=None,native=native,body_sha256=body_hash)
    return item


async def _authority(target,policy):
    if await target.fetchval('SELECT current_user')!='cortex_v2_migrator': raise ImportRefused('legacy_boot_migrator_required')
    if await target.fetchval('SELECT current_database()')!=policy['target_database']: raise ImportRefused('legacy_boot_target_database_mismatch')
    principal,installation=_uuid(policy['principal_id']),_uuid(policy['target_installation_id'])
    row=await target.fetchrow('''SELECT p.status AS principal_status,p.installation_id,i.status AS installation_status
        FROM cortex_auth.principals p JOIN cortex_auth.installations i USING(installation_id)
        WHERE p.principal_id=$1 FOR SHARE OF p,i''',principal)
    if row is None or row['principal_status']!='active' or row['installation_status']!='active' or row['installation_id']!=installation:
        raise ImportRefused('legacy_boot_authority_unavailable')
    for project in sorted(policy['projects'].values(),key=lambda p:p['scope_id']):
        scope=_uuid(project['scope_id'])
        row=await target.fetchrow('''SELECT s.scope_kind,g.can_write FROM cortex_core.scopes s
            JOIN cortex_auth.scope_grants g USING(scope_id) WHERE s.scope_id=$1 AND g.principal_id=$2 FOR SHARE OF s,g''',scope,principal)
        if row is None or not row['can_write'] or row['scope_kind']!=('shared' if project['scope']=='global' else 'project'):
            raise ImportRefused('legacy_boot_scope_unavailable')
        await target.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",'legacy.boot.scope:'+str(scope))


async def _native(target,item):
    kind=item['kind'];p=item['native']
    raw=await target.fetchval(f'SELECT to_jsonb(t)::text FROM cortex_context.{kind}_revisions t WHERE scope_id=$1 AND {kind}_id=$2 AND revision=$3',_uuid(p['scope_id']),_uuid(p[kind+'_id']),p['revision'])
    return None if raw is None else json.loads(raw)


async def _verify_rows(target,rows):
    originals={}
    for item in rows:
        raw=item['original'].encode()
        if hashlib.sha256(raw).hexdigest()!=item['source_sha256'] or item['source_reference'] in originals: raise ImportRefused('legacy_boot_original_drift')
        originals[item['source_reference']]=raw
        if item['outcome']=='migrated':
            actual=await _native(target,item)
            if actual!=item['native'] or hashlib.sha256(actual['body'].encode()).hexdigest()!=item['body_sha256']: raise ImportRefused('legacy_boot_native_drift')
        elif item['outcome']!='quarantined' or not item['reason'] or item['native'] is not None: raise ImportRefused('legacy_boot_disposition_drift')
    return originals


async def _insert(target,item):
    kind=item['kind'];p=item['native'];columns=list(p);values=[]
    for key in columns:
        value=p[key]
        if key in ('scope_id',kind+'_id','created_by_principal'):value=_uuid(value)
        elif key=='created_at':value=datetime.fromisoformat(value)
        elif key in ('boot_manifest','payload'):value=_json(value)
        values.append(value)
    placeholders=[f'${i}'+('::jsonb' if key in ('boot_manifest','payload') else '') for i,key in enumerate(columns,1)]
    await target.execute(f"INSERT INTO cortex_context.{kind}_revisions({','.join(columns)}) VALUES({','.join(placeholders)})",*values)


async def import_snapshot(target,snapshot,policy,*,idempotency_key,fault=None):
    approved=_inputs(snapshot,policy,idempotency_key)
    if target.is_in_transaction(): raise ImportRefused('legacy_boot_owns_transaction')
    rows=[_project(r,approved['records'][r.source_reference],approved['projects'],dict(snapshot.bodies),approved['principal_id']) for r in snapshot.records]
    digest=request_digest({'snapshot':snapshot.fingerprint,'policy':approved})
    try:
        async with target.transaction(isolation='read_committed'):
            await _authority(target,approved)
            previous,replayed=await begin_command(target,principal_id=_uuid(approved['principal_id']),installation_id=_uuid(approved['target_installation_id']),
                                                 operation=OPERATION,idempotency_key=idempotency_key,digest=digest)
            if replayed:
                await _verify_rows(target,previous['rows']);return previous
            predecessors={}
            prior=await target.fetch('SELECT receipt FROM cortex_core.command_receipts WHERE principal_id=$1 AND installation_id=$2 AND operation=$3',
                                     _uuid(approved['principal_id']),_uuid(approved['target_installation_id']),OPERATION)
            for stored in prior:
                old=json.loads(stored['receipt'])
                if old.get('schema')!='cortex.legacy-boot-import.v1': raise ImportRefused('legacy_boot_predecessor_receipt_unavailable')
                await _verify_rows(target,old['rows'])
                for old_item in old['rows']:
                    if old_item['outcome']=='migrated':
                        old_p=old_item['native'];old_kind=old_item['kind']
                        predecessors[(old_p['scope_id'],old_kind,old_p[old_kind+'_id'],old_p['revision'])]=old_item
            for item in rows:
                if item['outcome']!='migrated': continue
                p=item['native'];kind=item['kind']
                latest=await target.fetchval(f'SELECT MAX(revision) FROM cortex_context.{kind}_revisions WHERE scope_id=$1 AND {kind}_id=$2',_uuid(p['scope_id']),_uuid(p[kind+'_id']))
                if p['revision']==1:
                    if latest is not None: raise ImportRefused('legacy_boot_canonical_collision')
                else:
                    previous=predecessors.get((p['scope_id'],kind,p[kind+'_id'],p['revision']-1))
                    if latest!=p['revision']-1 or previous is None or previous['source_reference']!=item['source_reference']:
                        raise ImportRefused('legacy_boot_predecessor_unavailable')
            for item in rows:
                if item['outcome']=='migrated':await _insert(target,item)
            originals=await _verify_rows(target,rows)
            if originals!={r.source_reference:r.original_bytes for r in snapshot.records}:raise ImportRefused('legacy_boot_accounting_drift')
            receipt={'schema':'cortex.legacy-boot-import.v1','source_database':snapshot.database,'source_snapshot_sha256':snapshot.fingerprint,
                     'target_installation_id':approved['target_installation_id'],'target_database':approved['target_database'],'principal_id':approved['principal_id'],'idempotency_key':idempotency_key,
                     'counts':dict(sorted(Counter(row['outcome'] for row in rows).items())),
                     'functional_pass':all(row['outcome']=='migrated' for row in rows),'rows':rows}
            if len(_json(receipt).encode())>LIMIT: raise ImportRefused('legacy_boot_receipt_too_large')
            await commit_receipt(target,principal_id=_uuid(approved['principal_id']),installation_id=_uuid(approved['target_installation_id']),
                                 operation=OPERATION,idempotency_key=idempotency_key,digest=digest,receipt_kind='committed',receipt=receipt)
            if fault is not None:fault('before_commit')
            return receipt
    except ApiProblem:
        raise ImportRefused('legacy_boot_binding_conflict') from None
    except asyncpg.PostgresError:
        raise ImportRefused('legacy_boot_native_refused') from None


async def read_inverse(target,receipt):
    async with target.transaction(isolation='repeatable_read',readonly=True):
        if await target.fetchval('SELECT current_user')!='cortex_v2_migrator':raise ImportRefused('legacy_boot_migrator_required')
        if await target.fetchval('SELECT current_database()')!=receipt['target_database']:raise ImportRefused('legacy_boot_target_database_mismatch')
        raw=await target.fetchval('SELECT receipt FROM cortex_core.command_receipts WHERE principal_id=$1 AND operation=$2 AND idempotency_key=$3',
                                 _uuid(receipt['principal_id']),OPERATION,receipt['idempotency_key'])
        if raw is None or _json(json.loads(raw))!=_json(receipt):raise ImportRefused('legacy_boot_receipt_drift')
        return await _verify_rows(target,receipt['rows'])
