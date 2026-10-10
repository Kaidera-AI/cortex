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

# Source-reviewed, normalized ASTs of the complete enclosing functions. A change
# anywhere in one of these functions invalidates its indirect-SQL exception.
KNOWN_INPUT_FUNCTION_SHA256 = {
    ('src/cortex_core/coordination.py','Jobs._load'): 'e10eddc8f089d9ccc97e60e576dedbfe3d39541e3eb8bee0414db28b29407ade',
    ('src/cortex_core/auth.py','authorized'): '479d115551dc48de8bbc2e9fba943d21879bf0497f05a5d65610a97dc282ae46',
    ('src/cortex_core/identity.py','Identity._control'): '320f495f9fe9144201451e8d59117ac785efe629d24a518fc7365b685da100ba',
    ('src/cortex_core/identity.py','Identity._register'): 'f837fa6fdcc01e32d12b74eb9b8a1390a3305bd8ef256fdf28b6b3b460e820b8',
    ('src/cortex_core/records.py','_private'): '7b69423595de4126e83a8b345bf785b84f62510d05f451eac13981455d8f2e66',
    ('src/cortex_core/migrations.py','apply_migrations'): '7dbe9e12eaea949645208137e8db565874622c0af7d625529816e30aea3b5df3',
}

# The function pins alone cannot see an aliased caller elsewhere in the module.
# Every source change in a module that owns an exception requires a reviewed pin.
KNOWN_INPUT_FILE_SHA256 = {
    'src/cortex_core/coordination.py': '56c709da4e5caf8d5baceba92d08d60239d1aa07c85d61c1568bb74320d4a70b',
    'src/cortex_core/auth.py': 'cab6320af61cd8beff4923524460413ff3c48bc0ced61f9315919a7fb96567f0',
    'src/cortex_core/identity.py': 'aa5b4139e15a46d1eb161d84d23a3e9a2553e5126b110549d5bf839004a4ea1d',
    'src/cortex_core/records.py': '3f361acf476741724e4f8544e7235314c798104e64f54107020f83bb9325db9d',
    'src/cortex_core/migrations.py': 'b225584cbc1cd0e8c44af3e2861fd5f4c7f96f4107abade24f0dea38b80df1d7',
}
EXEMPT_SYMBOL_FILES = {
    '_private': {'src/cortex_core/records.py'},
    '_ROLE_QUERY': {'src/cortex_core/auth.py','src/cortex_core/identity.py'},
    '_register': {'src/cortex_core/identity.py'},
    'apply_migrations': {'src/cortex_core/migrations.py'},
    '_load': {'src/cortex_core/coordination.py'},
}


def normalized_function_digest(function):
    # 3.14 changed ast.dump's default to hide empty fields; keep the 3.12 form.
    options={'show_empty':True} if sys.version_info >= (3,14) else {}
    return hashlib.sha256(ast.dump(function,include_attributes=False,**options).encode()).hexdigest()


def source_reference_flags(file,source):
    """Unreviewed exempt-name references and dynamic reflection in NEXT source."""
    tree=ast.parse(source);flags=[];ordinals={}
    def flag(kind,node):
        ordinal=ordinals.get(kind,0);ordinals[kind]=ordinal+1
        flags.append(dict(file=file,owner=kind,verb='DYNAMIC',table='<dynamic_or_unsupported>',
                          ordinal=ordinal,statement_sha256=hashlib.sha256(
                              ast.dump(node,include_attributes=False).encode()).hexdigest(),classification=None))
    for node in ast.walk(tree):
        symbols=[]
        if isinstance(node,ast.Name):symbols=[node.id]
        elif isinstance(node,ast.Attribute):symbols=[node.attr]
        elif isinstance(node,ast.alias):symbols=[node.name,node.asname]
        elif isinstance(node,ast.Constant) and isinstance(node.value,str):symbols=[node.value]
        for symbol in symbols:
            if symbol in EXEMPT_SYMBOL_FILES and file not in EXEMPT_SYMBOL_FILES[symbol]:
                flag('cross_file:'+symbol,node)
        if isinstance(node,ast.Call):
            function=node.func;index=None;label=None
            if isinstance(function,ast.Name) and function.id in ('getattr','setattr','exec','eval','__import__'):
                label=function.id;index=1 if label in ('getattr','setattr') else 0
            elif (isinstance(function,ast.Attribute) and function.attr=='import_module'
                  and isinstance(function.value,ast.Name) and function.value.id=='importlib'):
                label='importlib.import_module';index=0
            if label is not None and (len(node.args)<=index or static_text(node.args[index]) is None):
                flag('dynamic_name:'+label,node)
    return flags


def static_text(node):
    """Fold source-known SQL without executing application expressions."""
    if isinstance(node,ast.Constant) and isinstance(node.value,str):return node.value
    if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add):
        left,right=static_text(node.left),static_text(node.right)
        if left is not None and right is not None:return left+right
    if isinstance(node,ast.JoinedStr):
        parts=[static_text(part) for part in node.values]
        if all(part is not None for part in parts):return ''.join(parts)
    return None


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
    if file=='schema/coordination/002-record-replay.sql' and owner=='coordination.c05_update_head' and table=='core.records' and verb=='UPDATE':
        return 'trigger_capture_and_sequential_head_guard'
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
    def add_dynamic(owner,node,force=False):
        fragments=[child.value for child in sorted(ast.walk(node),key=lambda part:(getattr(part,'lineno',0),getattr(part,'col_offset',0)))
                   if isinstance(child,ast.Constant) and isinstance(child.value,str)]
        candidate=''.join(fragments)
        if not force and not any(not (match.group().upper()=='UPDATE' and re.search(r'\bFOR\s*$',candidate[:match.start()],re.I))
                   for match in re.finditer(r'\b(?:INSERT|UPDATE|DELETE)\b',candidate,re.I)):return
        key=(owner,'DYNAMIC','<dynamic_or_unsupported>');ordinal=ordinals.get(key,0);ordinals[key]=ordinal+1
        rows.append(dict(file=file,owner=owner,verb='DYNAMIC',table='<dynamic_or_unsupported>',ordinal=ordinal,
            statement_sha256=hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest(),classification=None))
    def known_existing_input(owner,node,tree,function):
        """Only these source-proved existing indirect inputs bypass fail-closed."""
        file_pin=KNOWN_INPUT_FILE_SHA256.get(file)
        if file_pin is None or normalized_function_digest(tree)!=file_pin:
            return False
        pin=KNOWN_INPUT_FUNCTION_SHA256.get((file,owner))
        if function is None or pin is None or normalized_function_digest(function)!=pin:
            return False
        if file=='src/cortex_core/coordination.py' and owner=='Jobs._load':
            return (isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add)
                    and (static_text(node.left) or '').lstrip().upper().startswith('SELECT')
                    and isinstance(node.right,ast.IfExp)
                    and static_text(node.right.body)==' FOR UPDATE'
                    and static_text(node.right.orelse)=='')
        if not isinstance(node,ast.Name):return False
        name=node.id
        if name=='_ROLE_QUERY' and file in ('src/cortex_core/auth.py','src/cortex_core/identity.py'):
            assignments=[n for n in ast.walk(tree) if isinstance(n,(ast.Assign,ast.AnnAssign))
                         and any(isinstance(t,ast.Name) and t.id==name for t in
                                 (n.targets if isinstance(n,ast.Assign) else [n.target]))]
            all_stores=[n for n in ast.walk(tree) if isinstance(n,ast.Name) and n.id==name
                        and isinstance(n.ctx,(ast.Store,ast.Del))]
            declarations=[n for n in ast.walk(tree) if isinstance(n,(ast.Global,ast.Nonlocal))
                          and name in n.names]
            if declarations:return False
            if file.endswith('/auth.py'):
                return (len(assignments)==len(all_stores)==1
                        and all_stores[0] is assignments[0].targets[0]
                        and (static_text(assignments[0].value) or '').lstrip().upper().startswith('SELECT'))
            return (not assignments and not all_stores and any(isinstance(n,ast.ImportFrom) and n.module=='auth'
                        and any(a.name=='_ROLE_QUERY' for a in n.names) for n in tree.body))
        if file=='src/cortex_core/identity.py' and owner=='Identity._register' and name=='query':
            methods=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_register']
            assignments=[n for n in ast.walk(methods[0]) if isinstance(n,ast.Assign)
                         and any(isinstance(t,ast.Name) and t.id=='query' for t in n.targets)] if len(methods)==1 else []
            if len(methods)!=1:return False
            return (len(assignments)==2
                    and all((static_text(n.value) or '').lstrip().upper().startswith('SELECT') for n in assignments))
        if file=='src/cortex_core/records.py' and owner=='_private' and name=='query':
            methods=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_private']
            if len(methods)!=1:return False
            function=methods[0]
            parameters=[arg.arg for arg in (function.args.posonlyargs+function.args.args+
                                           function.args.kwonlyargs)]
            calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
                   and n.func.id=='_private']
            return parameters.count(name)==1 and bool(calls) and all(len(n.args)>1 and
                   (static_text(n.args[1]) or '').lstrip().upper().startswith('SELECT') for n in calls)
        if file=='src/cortex_core/migrations.py' and owner=='apply_migrations' and name=='sql':
            functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='apply_migrations']
            if len(functions)!=1:return False
            fn=functions[0]
            loops=[n for n in ast.walk(fn) if isinstance(n,ast.For) and isinstance(n.target,ast.Tuple)
                   and [getattr(t,'id',None) for t in n.target.elts]==['identity','digest','sql']
                   and isinstance(n.iter,ast.Name) and n.iter.id=='prepared']
            appends=[n for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
                     and n.func.attr=='append' and isinstance(n.func.value,ast.Name) and n.func.value.id=='prepared']
            other_assignments=[n for n in ast.walk(fn) if isinstance(n,(ast.Assign,ast.AnnAssign,ast.NamedExpr))
                               and any(isinstance(t,ast.Name) and t.id=='sql' for t in
                                       (n.targets if isinstance(n,ast.Assign) else [n.target]))]
            return (len(loops)==1 and len(appends)==1 and not other_assignments
                    and 'hashlib.sha256(data).hexdigest() != entry["sha256"]' in source
                    and ast.unparse(appends[0].args[0])=="(entry['id'], entry['sha256'], data.decode('utf-8'))")
        return False
    if file.endswith('.py'):
        tree=ast.parse(source)
        class Visitor(ast.NodeVisitor):
            def __init__(self):self.owners=[];self.functions=[]
            def visit_ClassDef(self,node):
                self.owners.append(node.name);self.generic_visit(node);self.owners.pop()
            def visit_FunctionDef(self,node):
                self.owners.append(node.name);self.functions.append(node)
                self.generic_visit(node)
                self.functions.pop();self.owners.pop()
            visit_AsyncFunctionDef=visit_FunctionDef
            def visit_Call(self,node):
                if (isinstance(node.func,ast.Attribute) and node.func.attr in ('execute','executemany')
                    and node.args and static_text(node.args[0]) is None):
                    owner='.'.join(self.owners) or '<module>'
                    if not known_existing_input(owner,node.args[0],tree,self.functions[-1] if self.functions else None):
                        add_dynamic(owner,node.args[0],force=True)
                self.generic_visit(node)
            def visit_BinOp(self,node):
                value=static_text(node)
                if value is not None:
                    add('.'.join(self.owners) or '<module>',value)
                else:self.generic_visit(node)
            def visit_JoinedStr(self,node):
                value=static_text(node)
                if value is not None:
                    add('.'.join(self.owners) or '<module>',value)
                else:self.generic_visit(node)
            def visit_Constant(self,node):
                if isinstance(node.value,str):add('.'.join(self.owners) or '<module>',node.value)
        Visitor().visit(tree)
    else:
        for function in FUNCTION.finditer(source):add(function.group(1),function.group(2))
    return rows


def scan(root,overrides=None):
    root=Path(root);overrides=overrides or {};rows=[]
    paths=sorted(list((root/'src/cortex_core').rglob('*.py'))+list((root/'schema').rglob('*.sql')))
    for path in paths:
        name=str(path.relative_to(root));rows.extend(scan_source(name,overrides.get(name,path.read_text())))
    for path in sorted((root/'src').rglob('*.py')):
        name=str(path.relative_to(root));rows.extend(source_reference_flags(name,overrides.get(name,path.read_text())))
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
