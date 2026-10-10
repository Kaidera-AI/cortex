"""Stdlib writer-site inventory. No runtime imports, credentials, or database calls."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

DML = re.compile(r'\b(INSERT\s+INTO|UPDATE(?!\s+SET\b)|DELETE\s+FROM)\b',re.I)
IDENTIFIER = r'(?:"(?:""|[^"])+"|[a-z_][a-z0-9_$]*)'
TARGET = re.compile(r'\s*('+IDENTIFIER+r'(?:\s*\.\s*'+IDENTIFIER+r')?)',re.I)
FUNCTION = re.compile(r'CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+([a-z_][a-z0-9_.]*).*?\bAS\s+\$\$(.*?)\$\$;',re.I|re.S)


def category(file,owner,verb,table):
    if table.startswith('retrieval.') and ('embeddings/' in file or 'modules/graph/' in file or file.startswith('schema/retrieval/')):
        return 'derived_projection_or_cache; separate module admission held'
    if table=='coordination.supervisor_leases' and ('conductor/' in file or 'c08-supervisor' in file):
        return 'optional_C08_control_plane; outside canonical mutation feed'
    if file=='src/cortex_core/migrations.py' and table=='core.schema_migrations' and verb=='INSERT':
        return 'explicit_installer_only; never runtime mutation'
    if file=='src/cortex_core/records.py':
        rules={('_payload','core.payloads','INSERT'):'immutable_payload_subordinate',
          ('_append_revision','core.record_revisions','INSERT'):'trigger_capture_and_deferred_history_guard',
          ('_save_request','coordination.idempotency','INSERT'):'immutable_current_event_receipt_guard',
          ('Records._mutate','core.records','INSERT'):'trigger_capture_and_sequential_head_guard',
          ('Records._mutate','core.records','UPDATE'):'trigger_capture_and_sequential_head_guard'}
        return rules.get((owner,table,verb))
    if file=='src/cortex_core/coordination.py':
        allowed={('_append_result','coordination.job_results','INSERT'),('Jobs.create.create','coordination.jobs','INSERT'),
          ('Jobs.claim.claim','coordination.leases','INSERT'),('Jobs.claim.claim','coordination.job_attempts','INSERT'),
          ('Jobs._state','coordination.jobs','UPDATE'),('Jobs._state','coordination.leases','UPDATE'),
          ('Jobs._replace_metadata','coordination.jobs','UPDATE')}
        if (owner,table,verb) in allowed:return 'job_parent_snapshot_before_receipt; deferred_final_snapshot_guard'
    if file=='schema/auth/002-isolation.sql' and owner=='auth.bind_scope' and table=='pg_temp.c04_request_context':
        return 'private_transaction_binding_housekeeping'
    if file=='schema/auth/002-isolation.sql' and owner=='auth.bump_generation' and table=='auth.permission_generations':
        return 'authorization_generation_housekeeping'
    if file=='schema/auth/003-identity.sql':
        if owner in ('auth.identity_register','auth.identity_set_roles','auth.identity_rotate','auth.identity_revoke','auth.identity_adopt') and table in ('auth.principals','auth.project_grants','auth.credentials'):
            return 'fixed_identity_authority; captured_by_additive_identity_finish'
        if owner=='auth.identity_finish' and table in ('core.payloads','coordination.idempotency'):
            return 'original_immutable_audit_and_receipt; additive_finish_captures_every_target'
    if file=='schema/coordination/002-outbox.sql':
        rules={'coordination.c06_capture_revision':{'coordination.outbox'},
          'coordination.c06_fact':{'core.records','core.record_aliases','core.record_revisions'},
          'coordination.capture_job':{'core.payloads'},
          'auth.identity_finish':{'core.payloads','coordination.idempotency'},
          'coordination.publish_outbox':{'coordination.feed_state','coordination.published_events'},
          'coordination.prune_outbox':{'coordination.feed_state','coordination.consumer_checkpoints','coordination.published_events','coordination.quarantine','coordination.outbox'},
          'coordination.c06_protection_lock':{'coordination.feed_state'}}
        if table in rules.get(owner,set()):return 'private_C06_'+owner.rsplit('.',1)[-1]
    return None


def scan_source(file,source):
    rows=[]; ordinals={}
    def add(owner,text):
        for match in DML.finditer(text):
            verb=match.group(1).split()[0].upper()
            if text[match.start()-1:match.start()]=="'" and text[match.end():match.end()+1]=="'":continue
            if verb=='UPDATE' and re.search(r'\bFOR\s*$',text[:match.start()],re.I):continue
            target=TARGET.match(text,match.end())
            table='<dynamic_or_unsupported>' if target is None else re.sub(r'\s+','',target.group(1)).replace('"','').lower()
            if '.' not in table:table='<unqualified>:'+table
            key=(owner,verb,table);ordinal=ordinals.get(key,0);ordinals[key]=ordinal+1
            normalized=' '.join(text.split())
            rows.append(dict(file=file,owner=owner,verb=verb,table=table,ordinal=ordinal,
                statement_sha256=hashlib.sha256(normalized.encode()).hexdigest(),classification=category(file,owner,verb,table)))
    if file.endswith('.py'):
        class Visitor(ast.NodeVisitor):
            def __init__(self):self.owners=[]
            def visit_ClassDef(self,node):
                self.owners.append(node.name);self.generic_visit(node);self.owners.pop()
            def visit_FunctionDef(self,node):
                self.owners.append(node.name);self.generic_visit(node);self.owners.pop()
            visit_AsyncFunctionDef=visit_FunctionDef
            def visit_Constant(self,node):
                if isinstance(node.value,str):add('.'.join(self.owners) or '<module>',node.value)
        Visitor().visit(ast.parse(source))
    else:
        for function in FUNCTION.finditer(source):add(function.group(1),function.group(2))
    return rows


def scan(root,overrides=None):
    root=Path(root);overrides=overrides or {};rows=[]
    paths=sorted(list((root/'src/cortex_core').rglob('*.py'))+list((root/'schema').rglob('*.sql')))
    for path in paths:
        name=str(path.relative_to(root));rows.extend(scan_source(name,overrides.get(name,path.read_text())))
    return sorted(rows,key=lambda r:(r['file'],r['owner'],r['verb'],r['table'],r['ordinal']))


def audit(root,inventory=None,overrides=None):
    root=Path(root);inventory=inventory if inventory is not None else json.loads((root/'contracts/core-writer-inventory.json').read_bytes())
    actual=scan(root,overrides);declared=inventory.get('writer_sites',[])
    unclassified=[r for r in actual if r['classification'] is None]
    missing=[r for r in actual if r not in declared];stale=[r for r in declared if r not in actual]
    return dict(passed=bool(actual) and not unclassified and not missing and not stale and inventory.get('unclassified_writers')==[],
        writer_count=len(actual),unclassified=unclassified,missing=missing,stale=stale)


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    if '--bind' in sys.argv:
        p=root/'contracts/core-writer-inventory.json';inventory=json.loads(p.read_bytes());inventory['writer_sites']=scan(root)
        inventory['unclassified_writers']=[r for r in inventory['writer_sites'] if r['classification'] is None]
        p.write_text(json.dumps(inventory,indent=2)+'\n')
    result=audit(root);print(json.dumps(result));raise SystemExit(0 if result['passed'] else 1)
