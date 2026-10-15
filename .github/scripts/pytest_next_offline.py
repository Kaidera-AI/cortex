"""Inventory every next/ suite, then require complete offline per-file outcomes."""
import argparse
import ast
from collections import Counter
import errno
import hashlib
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tempfile
import time
import shutil

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / '.github/next-offline-suites.json'
ACCEPTED_BASE = 'a47a152ba4eb048f4a9ff211b875d59d82a2e066'
# One-time initial integration: this exact main predates the inventory file.
BOOTSTRAP_MAIN = '66cba6a1f8a2a3787844e168ab5415cb96738a0d'
# Kai ruling359622c6: immutable five-row policy, retained in this branch's history.
UNBUILT_POLICY = '741c43e41f46ba2e818d3bde8581fff9b27abc9c'
SPEC = importlib.util.spec_from_file_location('unchanged_u13_outcome',
                                            Path(__file__).with_name('pytest_u13_worker.py'))
U13 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(U13)


def suite_name(name):
    return name.endswith('.py') and (name.startswith('test_') or name.endswith('_test.py'))


def retirement_require(condition, code):
    if not condition:
        raise ValueError('retirement: ' + code)


def source_tree(path):
    try:
        return ast.parse(path.read_text()), True
    except (OSError, UnicodeError, SyntaxError):
        return ast.Module(body=[], type_ignores=[]), False


def named_nodes(path, tree):
    nodes = [str(path) + '::' + node.name for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')]
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef):
            nodes.extend(str(path) + '::' + cls.name + '::' + node.name for node in cls.body
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'))
    return sorted(nodes)


def retirement_inventory(root, run, bindings):
    """Read existing authority literals, never execute their unittest runners."""
    digest = lambda path: hashlib.sha256((root/path).read_bytes()).hexdigest() if (root/path).is_file() else None
    sha = lambda value: isinstance(value, str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)
    retirement_require(isinstance(bindings, list) and all(isinstance(row, dict)
        and set(row)=={'record','sha256','files'} and isinstance(row['record'], str)
        and sha(row['sha256']) and isinstance(row['files'], dict)
        and all(isinstance(path, str) and sha(pin) for path,pin in row['files'].items())
        for row in bindings), 'binding_schema')
    records = {}
    for path in sorted((root/'next/tests').rglob('run_active.py')):
        tree, parsed = source_tree(path)
        retirement_require(parsed, 'record_parse')
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id in ('RETIRED','retired') for target in node.targets)]
        if values:
            records[str(path.relative_to(root))] = values
    retirement_require(len(bindings)==len({row['record'] for row in bindings})
        and {row['record'] for row in bindings}==set(records), 'record_inventory')
    retired, replacements = [], {}
    for binding in bindings:
        path = binding['record']
        retirement_require(digest(path)==binding['sha256'], 'record_hash')
        values = records.get(path, [])
        literal, unique = {}, False
        if len(values)==1 and isinstance(values[0], ast.Dict):
            try:
                keys = [ast.literal_eval(key) for key in values[0].keys]
                literal = ast.literal_eval(values[0])
                unique = len(keys)==len(set(keys))
            except (ValueError, TypeError):
                pass
        retirement_require(unique and isinstance(literal, dict) and bool(literal)
            and all(isinstance(key, str) and len(key.split('.'))==3
                    and all(part.isidentifier() for part in key.split('.'))
                    and key.split('.')[-1].startswith('test_') and isinstance(reason, str)
                    and bool(reason.strip()) and len(reason.splitlines())==1
                    for key,reason in literal.items()), 'record_literal')
        originals = {str(Path(path).parent/(name.split('.')[0]+'.py')) for name in literal}
        pairs = {original:original[:-3]+'_v2.py' for original in originals}
        retirement_require(set(binding['files'])==originals|set(pairs.values()), 'file_bindings')
        retirement_require(all(digest(file)==pin for file,pin in binding['files'].items()), 'file_hash')
        retirement_require(all(file in run for file in originals|set(pairs.values())), 'live_suite')
        for original,replacement in pairs.items():
            tree, parsed = source_tree(root/replacement)
            nodes = named_nodes(replacement, tree)
            retirement_require(parsed and bool(nodes), 'replacement_nodes')
            replacements[replacement] = nodes
        for name,reason in literal.items():
            module,cls,method = name.split('.')
            original = str(Path(path).parent/(module+'.py'))
            tree, parsed = source_tree(root/original)
            node = original+'::'+cls+'::'+method
            retirement_require(parsed and node in named_nodes(original, tree), 'original_node')
            retired.append(dict(nodeid=node, reason=reason, record=path,
                record_sha256=binding['sha256'], source_sha256=binding['files'].get(original),
                replacement_nodes=replacements[pairs[original]]))
    return retired, replacements


def base_manifest_at(root, ref):
    if not isinstance(ref,str) or len(ref)!=40 or any(c not in '0123456789abcdef' for c in ref):
        raise ValueError('coverage: invalid independent base SHA')
    try:
        subprocess.check_output(['git','cat-file','-e',ref+'^{commit}'],cwd=root,stderr=subprocess.DEVNULL)
        policy_ref=ref
        if ref==BOOTSTRAP_MAIN and not subprocess.check_output(
                ['git','ls-tree','--name-only',ref,'--','.github/next-offline-suites.json'],cwd=root):
            # Do not bootstrap unknown refs, missing commits, or malformed manifests.
            policy_ref=ACCEPTED_BASE
        return json.loads(subprocess.check_output(['git','show',policy_ref+':.github/next-offline-suites.json'],
                                                 cwd=root,stderr=subprocess.DEVNULL))
    except (OSError,ValueError,subprocess.CalledProcessError):
        raise ValueError('coverage: independent base unavailable') from None


def source_path(name):
    return (isinstance(name,str) and name.startswith('next/') and name.endswith('.py')
            and not any(c in name for c in '*?[]\\')
            and not any(part in ('','.','..') for part in name.split('/')))


def symbol(tree, name):
    found=tree
    for part in name.split('.'):
        matches=[node for node in getattr(found,'body',[]) if isinstance(node,
            (ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and node.name==part]
        if len(matches)!=1:return None
        found=matches[0]
    return found


def assertions(node):
    if node is None:return []
    return [ast.unparse(item) for item in ast.walk(node) if isinstance(item,ast.Assert)
        or isinstance(item,ast.Call) and isinstance(item.func,ast.Attribute)
        and isinstance(item.func.value,ast.Name) and item.func.value.id=='self'
        and item.func.attr.startswith('assert')]


def fixture_evidence(root, suite, evidence, nodeid=None):
    """Conservative AST proof; unknown or mixed coverage cannot leave RUN."""
    if not isinstance(evidence,dict) or set(evidence) not in (
            {'kind','source','sha256','symbol'},{'kind','source','sha256','symbol','keys'}):return False
    name=evidence['source']
    if not source_path(name) or not isinstance(evidence['symbol'],str):return False
    path=root/name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=evidence['sha256']:return False
    tree,parsed=source_tree(path);anchor=symbol(tree,evidence['symbol'])
    if not parsed or anchor is None:return False
    candidate,parsed=source_tree(root/suite)
    if not parsed:return False
    if evidence['kind']=='unbuilt_unit':
        # The same source reader accepts only the five explicitly ratified rows.
        approved=base_manifest_at(ROOT,UNBUILT_POLICY).get('method_exclusions',[])
        return any(row['nodeid']==nodeid and row['evidence']==evidence
            and row['evidence'].get('kind')=='unbuilt_unit' for row in approved)
    if evidence['kind'] in ('postgres_probe_fixture','loopback_fixture'):
        if name!=suite or not nodeid or evidence['symbol']!='.'.join(nodeid.split('::')[1:]):return False
        if not isinstance(anchor,(ast.FunctionDef,ast.AsyncFunctionDef)) or not anchor.name.startswith('test_'):return False
        calls=[item for item in ast.walk(anchor) if isinstance(item,ast.Call)]
        if evidence['kind']=='loopback_fixture':
            return set(evidence)=={'kind','source','sha256','symbol'} and any(
                ast.unparse(call.func)=='asyncio.start_server' and len(call.args)>=3
                and isinstance(call.args[1],ast.Constant) and call.args[1].value=='127.0.0.1'
                and isinstance(call.args[2],ast.Constant) and call.args[2].value==0 for call in calls)
        reads={item.slice.value for item in ast.walk(anchor) if isinstance(item,ast.Subscript)
            and ast.unparse(item.value)=='os.environ' and isinstance(item.slice,ast.Constant)}
        # Actual named factory plus server-side query, not an unused DSN/import or a mocked connect.
        factory=any(isinstance(item,ast.Assign) and len(item.targets)==1
            and isinstance(item.targets[0],ast.Name) and item.targets[0].id=='factory'
            and isinstance(item.value,ast.Call) and ast.unparse(item.value.func)=='PostgresProbeFactory'
            and len(item.value.args)==1 and ast.unparse(item.value.args[0])=="os.environ['TEST_PROBE_DATABASE_URL']"
            for item in ast.walk(anchor))
        return (evidence.get('keys')==['TEST_PROBE_DATABASE_URL'] and reads=={'TEST_PROBE_DATABASE_URL'}
            and factory and any(ast.unparse(call.func)=='factory.connect' for call in calls)
            and any(ast.unparse(call.func)=='connection.execute' and call.args
                and isinstance(call.args[0],ast.Constant) and call.args[0].value=='SHOW statement_timeout' for call in calls))
    if evidence['kind'] in ('gated_env','external_artifact'):
        if name!=suite or not nodeid:return False
        selected=symbol(candidate,'.'.join(nodeid.split('::')[1:]))
        if selected is None:return False
        keys=evidence.get('keys')
        if not isinstance(keys,list) or not keys or len(set(keys))!=len(keys) or not all(isinstance(k,str) for k in keys):return False
        if evidence['kind']=='gated_env':
            if not isinstance(anchor,ast.ClassDef) or nodeid.split('::')[1]!=anchor.name or not any(isinstance(n,ast.FunctionDef) and n.name==selected.name for n in anchor.body):return False
            gates=[d for d in anchor.decorator_list if isinstance(d,ast.Call)
                   and ast.unparse(d.func)=='unittest.skipUnless' and d.args]
            return len(keys)==1 and any(isinstance(d.args[0],ast.Compare)
                and ast.unparse(d.args[0])==f"os.environ.get({keys[0]!r}) == '1'" for d in gates)
        if evidence['symbol']!='.'.join(nodeid.split('::')[1:]):return False
        reads={item.slice.value for item in ast.walk(anchor) if isinstance(item,ast.Subscript)
            and ast.unparse(item.value)=='os.environ' and isinstance(item.slice,ast.Constant)
            and isinstance(item.slice.value,str)}
        calls={ast.unparse(item.func) for item in ast.walk(anchor) if isinstance(item,ast.Call)}
        return set(keys)==reads and any(call.endswith(('.read_bytes','.read_text')) for call in calls)
    if evidence['kind']!='postgres_fixture' or not isinstance(anchor,(ast.FunctionDef,ast.AsyncFunctionDef)) or anchor.name!='setUp':return False
    driver_names={alias.asname or alias.name for item in tree.body if isinstance(item,ast.Import)
                  for alias in item.names if alias.name in ('psycopg','psycopg2','asyncpg')}
    required=any(isinstance(item,(ast.Assign,ast.Expr)) and isinstance(item.value,ast.Call)
        and isinstance(item.value.func,ast.Attribute) and item.value.func.attr=='connect'
        and isinstance(item.value.func.value,ast.Name) and item.value.func.value.id in driver_names
        for item in anchor.body)
    if not required:return False
    anchor_class=evidence['symbol'].split('.')[0]
    def inherits(file,cls,seen):
        key=(file,cls.name)
        if key in seen or len(seen)>32:return False
        seen=seen|{key}
        if file==name and cls.name==anchor_class:return True
        setup=next((n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='setUp'),None)
        delegated=[] if setup is None else [n.value.func.value for n in setup.body
            if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
            and isinstance(n.value.func,ast.Attribute) and n.value.func.attr=='setUp']
        current,_=source_tree(root/file)
        # Existing Fixture delegates an unconditional setup to a module-qualified class.
        for call in delegated:
            if not isinstance(call,ast.Attribute) or not isinstance(call.value,ast.Name):continue
            for item in current.body:
                if not isinstance(item,ast.Import):continue
                for alias in item.names:
                    if (alias.asname or alias.name)!=call.value.id:continue
                    matches=list((root/'next').rglob(alias.name.split('.')[-1]+'.py'))
                    if len(matches)!=1:continue
                    target=str(matches[0].relative_to(root));imported,_=source_tree(matches[0]);other=symbol(imported,call.attr)
                    if isinstance(other,ast.ClassDef) and inherits(target,other,seen):return True
        for base in cls.bases:
            if not isinstance(base,ast.Name):continue
            if setup is not None and not any(
                isinstance(call,ast.Name) and call.id==base.id or
                cls.bases[0] is base and isinstance(call,ast.Call) and isinstance(call.func,ast.Name)
                and call.func.id=='super' and not call.args and not call.keywords for call in delegated):continue
            local=symbol(current,base.id)
            if isinstance(local,ast.ClassDef) and inherits(file,local,seen):return True
            for item in current.body:
                if not isinstance(item,ast.ImportFrom) or not item.module:continue
                for alias in item.names:
                    if (alias.asname or alias.name)!=base.id:continue
                    matches=list((root/'next').rglob(item.module.split('.')[-1]+'.py'))
                    if len(matches)!=1:continue
                    target=str(matches[0].relative_to(root));imported,_=source_tree(matches[0]);other=symbol(imported,alias.name)
                    if isinstance(other,ast.ClassDef) and inherits(target,other,seen):return True
        return False
    if nodeid:
        parts=nodeid.split('::')
        if len(parts)!=3 or parts[0]!=suite or nodeid not in named_nodes(suite,candidate):return False
        classes=[n for n in candidate.body if isinstance(n,ast.ClassDef) and n.name==parts[1]]
    else:
        if any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_') for n in candidate.body):return False
        classes=[n for n in candidate.body if isinstance(n,ast.ClassDef) and
                 (any(isinstance(m,(ast.FunctionDef,ast.AsyncFunctionDef)) and m.name.startswith('test_') for m in n.body)
                  or any(isinstance(b,ast.Name) and b.id not in ('object','TestCase') for b in n.bases))]
    return bool(classes) and all(inherits(suite,cls,set()) for cls in classes)


def semantic_retirements(root, retired, replacements, checks):
    retirement_require(isinstance(checks,list),'semantic_schema')
    live={node for nodes in replacements.values() for node in nodes}
    retirement_require(all(isinstance(row,dict) and set(row)=={'original','replacement','mutation'} for row in checks),'semantic_schema')
    declared=[];originals=[]
    allowed={row['nodeid']:set(row['replacement_nodes']) for row in retired}
    for row in checks:
        for key in ('original','replacement'):
            ref=row[key]
            retirement_require(isinstance(ref,dict) and set(ref)=={'nodeid','assertion'}
                and isinstance(ref['nodeid'],str) and isinstance(ref['assertion'],str),'semantic_schema')
            parts=ref['nodeid'].split('::');retirement_require(len(parts)==3 and source_path(parts[0]),'semantic_node')
            tree,parsed=source_tree(root/parts[0]);method=symbol(tree,'.'.join(parts[1:]))
            retirement_require(parsed and ref['assertion'] in assertions(method),'deciding_assertion')
            if key=='replacement':
                calls=[n for n in ast.walk(method) if isinstance(n,ast.Call) and not
                    (isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name) and n.func.value.id=='self')]
                retirement_require(bool(calls) and bool(assertions(method)),'nontrivial_replacement')
        retirement_require(row['replacement']['nodeid'] in allowed.get(row['original']['nodeid'],set()),'semantic_pair')
        mutation=row['mutation']
        retirement_require(isinstance(mutation,dict) and set(mutation)=={'path','sha256','before','after'}
            and source_path(mutation['path']) and mutation['path'].startswith('next/src/')
            and all(isinstance(mutation[k],str) and mutation[k] for k in ('sha256','before','after'))
            and mutation['before']!=mutation['after'],'mutation_binding')
        path=root/mutation['path'];raw=path.read_bytes() if path.is_file() else b''
        retirement_require(hashlib.sha256(raw).hexdigest()==mutation['sha256']
            and raw.decode().count(mutation['before'])==1,'mutation_binding')
        try:compile(raw.decode().replace(mutation['before'],mutation['after'],1),str(path),'exec')
        except (SyntaxError,ValueError):raise ValueError('retirement: mutation_binding') from None
        declared.append(row['replacement']['nodeid']);originals.append(row['original']['nodeid'])
    retirement_require(len(declared)==len(set(declared)) and set(declared)==live
        and set(originals)=={row['nodeid'] for row in retired},'semantic_coverage')
    return checks


def inventory(root, manifest, base_manifest=None):
    root = Path(root).resolve()
    found = sorted(str(p.relative_to(root)) for p in (root / 'next').rglob('*.py')
                   if suite_name(p.name))
    invalid, entries, run, excluded = [], [], [], []
    # rglob does not descend into directory symlinks: reject them rather than hide suites.
    for path in (root / 'next').rglob('*'):
        if path.is_symlink():
            invalid.append('symlink under next inventory: ' + str(path.relative_to(root)))
    if not isinstance(manifest, dict) or not {'run','excluded'}<=set(manifest) or not set(manifest)<= {'run','excluded','retirements','method_exclusions','retirement_checks'}:
        invalid.append('manifest must contain run/excluded and optional retirement source bindings')
        manifest = manifest if isinstance(manifest, dict) else {}
    for key in ('run', 'excluded'):
        values = manifest.get(key)
        if not isinstance(values, list):
            invalid.append(key + ' must be a list')
            continue
        for row in values:
            path = row if key == 'run' else row.get('path') if isinstance(row, dict) else None
            if (not isinstance(path, str) or not path.startswith('next/')
                    or any(c in path for c in '*?[]\\')
                    or any(part in ('', '.', '..') for part in path.split('/'))
                    or not suite_name(PurePosixPath(path).name)):
                invalid.append('invalid explicit suite path: ' + repr(path))
                continue
            entries.append(path)
            if key == 'run':
                run.append(path)
            else:
                reason = row.get('reason')
                if (set(row) not in ({'path', 'reason'},{'path','reason','evidence'}) or not isinstance(reason, str)
                        or not reason.strip() or len(reason.splitlines()) != 1):
                    invalid.append('missing or non-single-line exclusion reason: ' + path)
                excluded.append(row)
            absolute = root / path
            if absolute.is_symlink() or not absolute.resolve().is_relative_to(root / 'next'):
                invalid.append('suite symlink or escape: ' + path)
    duplicates = sorted(path for path, count in Counter(entries).items() if count != 1)
    if not run:
        invalid.append('RUN manifest must not be empty')
    if not (root / 'next').is_dir():
        invalid.append('next source root is absent')
    missing, stale = sorted(set(found) - set(entries)), sorted(set(entries) - set(found))
    retired, replacements = [], {}
    if not invalid:
        try:
            retired, replacements = retirement_inventory(root, run, manifest.get('retirements', []))
        except ValueError as error:
            invalid.append(str(error))
    transitions,method_exclusions,checks=[],[],[]
    if not invalid:
        try:
            base=base_manifest if base_manifest is not None else base_manifest_at(ROOT,ACCEPTED_BASE)
            if not isinstance(base,dict) or not isinstance(base.get('run'),list) or not base['run'] or not all(isinstance(p,str) for p in base['run']) or not isinstance(base.get('excluded'),list):
                raise ValueError('coverage: invalid independent base manifest')
            excludes={row['path']:row for row in excluded}
            for path in sorted(set(base['run'])-set(run)):
                row=excludes.get(path,{})
                valid=fixture_evidence(root,path,row.get('evidence'))
                transitions.append(dict(path=path,reason=row.get('reason'),evidence=row.get('evidence'),evidence_valid=valid))
                if not valid:invalid.append('coverage: RUN->EXCLUDED lacks required fixture proof: '+path)
            accepted={row['path']:row for row in base['excluded']}
            for row in excluded:
                if row['path'] not in base['run'] and (row['path'] not in accepted or row['reason']!=accepted[row['path']]['reason']):
                    if not fixture_evidence(root,row['path'],row.get('evidence')):
                        invalid.append('coverage: new or changed exclusion lacks fixture proof: '+row['path'])
            approved={row['record']:row for row in base.get('retirements',[])}
            for row in manifest.get('retirements',[]):
                previous=approved.get(row['record'])
                retirement_require(previous is not None,'accepted_base_retirement')
                retirement_require(row['sha256']==previous['sha256'],'accepted_base_record')
                retirement_require(row['files']==previous['files'],'accepted_base_files')
            method_exclusions=manifest.get('method_exclusions',[])
            if not isinstance(method_exclusions,list):raise ValueError('coverage: method exclusion schema')
            nodes=[]
            for row in method_exclusions:
                if not isinstance(row,dict) or set(row)!= {'nodeid','reason','evidence'} or not isinstance(row['nodeid'],str) or not isinstance(row['reason'],str) or not row['reason'].strip() or len(row['reason'].splitlines())!=1:
                    raise ValueError('coverage: method exclusion schema')
                node=row['nodeid'];path=node.split('::')[0]
                if row.get('evidence',{}).get('kind')=='unbuilt_unit':
                    approved=base_manifest_at(ROOT,UNBUILT_POLICY).get('method_exclusions',[])
                    if row not in approved:invalid.append('coverage: unbuilt declaration lacks exact ruling approval: '+node)
                if not source_path(path) or path not in run:
                    invalid.append('coverage: method exclusion path outside RUN: '+node)
                    continue
                tree,parsed=source_tree(root/path)
                if not parsed or node not in named_nodes(path,tree) or not fixture_evidence(root,path,row['evidence'],node):
                    invalid.append('coverage: method exclusion lacks fixture proof: '+node)
                nodes.append(node)
            if len(nodes)!=len(set(nodes)) or set(nodes)&{r['nodeid'] for r in retired}:raise ValueError('coverage: duplicate excluded/retired method')
            if retired:checks=semantic_retirements(root,retired,replacements,manifest.get('retirement_checks',[]))
        except (ValueError,OSError,KeyError,TypeError) as error:
            invalid.append(str(error) if isinstance(error,ValueError) else 'coverage: malformed source evidence')
    oracle = 'next/docs/runtime/rc0/test_oracle.py'
    return dict(ok=not (invalid or duplicates or missing or stale), found=found,
                run=run, excluded=excluded, missing=missing, stale=stale,
                duplicates=duplicates, invalid=invalid, rc0_oracle_present=oracle in found,
                retired=retired, replacement_nodes=replacements,transitions=transitions,
                method_exclusions=method_exclusions,retirement_checks=checks)


def declaration_observations(packet, excluded):
    """Executed negative evidence, never a skip/xfail or a passing test."""
    result=[]
    for declaration in excluded:
        if declaration.get('evidence',{}).get('kind')!='unbuilt_unit':continue
        node=declaration['nodeid'];rows=[r for r in packet.get('reports',[]) if r['nodeid']==node]
        calls=[r for r in rows if r['when']=='call']
        valid=(Counter(r['when'] for r in rows)==Counter(('setup','call','teardown'))
            and all(not r['wasxfail'] and r['outcome']==('failed' if r['when']=='call' else 'passed') for r in rows)
            and len(calls)==1 and calls[0].get('assertion') is True
            and calls[0].get('exception_type','').split('.')[-1] in ('SurfaceAbsent','SemanticRed'))
        result.append(dict(nodeid=node,reason=declaration['reason'],executed=bool(calls),
            outcome=calls[0]['outcome'] if len(calls)==1 else 'invalid',
            exception_type=calls[0].get('exception_type','') if len(calls)==1 else '',valid=valid,pass_credit=0))
    return result


def receipt_valid(packet, process_exit, retired=(), replacements=(), excluded=()):
    if not isinstance(packet,dict):return False
    try:
        unbuilt=[row for row in excluded if row.get('evidence',{}).get('kind')=='unbuilt_unit']
        fixtures=[row for row in excluded if row not in unbuilt]
        unavailable={row['nodeid'] for row in unbuilt}
        witnesses=declaration_observations(packet,excluded)
        if (packet.get('declared_unavailable',[])!=witnesses or not all(w['valid'] for w in witnesses)
            or len(unavailable)!=len(unbuilt) or not unavailable<=set(packet['expected'])):return False
        expected=[n for n in packet['expected'] if n not in unavailable]
        # Keep raw records intact; validate ordinary live phases using the existing U13 contract.
        live={**packet,'selected':[n for n in packet['selected'] if n not in unavailable],
              'reports':[r for r in packet['reports'] if r['nodeid'] not in unavailable],
              'pytest_exit':0 if unbuilt and packet['pytest_exit']==1 else packet['pytest_exit']}
        complete=(U13.valid(expected,live) if expected else bool(unbuilt) and not live['selected']
                  and not live['reports'] and not live['deselected'] and live['pytest_exit']==0)
        return (process_exit==0 and packet['gate_exit']==0 and complete
            and (not unbuilt or packet['pytest_exit']==1)
            and packet.get('retired',[])==list(retired) and packet.get('excluded_methods',[])==list(excluded)
            and not (set(packet['expected']) & {r['nodeid'] for r in [*retired,*fixtures]})
            and (not (retired or excluded) or Counter(packet.get('collected',[]))==
                 Counter(packet['expected']+[r['nodeid'] for r in [*retired,*fixtures]]))
            and (not replacements or sorted(packet['expected'])==sorted(replacements)))
    except (KeyError,TypeError,ValueError):return False


def product_observed(packet,path,pin):
    try:
        row=packet['observed_product']
        return (row['path']==path and row['sha256']==pin and row['source_unchanged'] is True
            and row['loaded_source_matches'] is True and isinstance(row['calls'],list) and bool(row['calls']))
    except (KeyError,TypeError):return False


def proof_killed(packet,process_exit,node,path,pin):
    try:
        rows=packet['reports']
        return (process_exit==packet['pytest_exit']==packet['gate_exit']==1
            and packet['expected']==packet['selected']==packet['collected']==[node]
            and not packet['deselected'] and not packet.get('retired',[]) and not packet.get('excluded_methods',[])
            and Counter((row['nodeid'],row['when']) for row in rows)==Counter((node,p) for p in ('setup','call','teardown'))
            and all(row['wasxfail'] is False and row['outcome']==('failed' if row['when']=='call' else 'passed')
                and (row['when']!='call' or row['assertion'] is True) for row in rows)
            and product_observed(packet,path,pin))
    except (KeyError,TypeError,ValueError):return False


def retirement_proofs(root,checks,destination,case,environment):
    """Use the existing worker twice per deciding node, on one owned source copy."""
    copied=case/'retirement-proof-source';destination.mkdir()
    shutil.copytree(root/'next',copied/'next',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(root/'.github/scripts',copied/'.github/scripts',ignore=shutil.ignore_patterns('__pycache__','*.pyc','tests'))
    worker=copied/'.github/scripts/pytest_next_worker.py';rows=[]
    for index,check in enumerate(checks):
        node=check['replacement']['nodeid'];mutation=check['mutation'];product=copied/mutation['path']
        original=product.read_bytes();mutated=original.decode().replace(mutation['before'],mutation['after'],1).encode()
        outcomes=[]
        try:
            for label,raw in [('baseline',original),('mutant',mutated)]:
                product.write_bytes(raw);name=f'{index:02d}-{label}';output=destination/(name+'.json')
                command=[sys.executable,'-B',str(worker),'--suite',node.split('::')[0],'--nodes-json',json.dumps([node]),
                    '--observe-product',mutation['path'],'--output',str(output),'--temporary',str(case/name)]
                process=subprocess.run(command,cwd=copied,env=environment,capture_output=True,text=True,timeout=180)
                (destination/(name+'.txt')).write_text(process.stdout+process.stderr)
                try:packet=json.loads(output.read_text())
                except (OSError,ValueError):packet=None
                pin=hashlib.sha256(raw).hexdigest()
                ok=(receipt_valid(packet,process.returncode,replacements=[node]) and product_observed(packet,mutation['path'],pin)) if label=='baseline' else proof_killed(packet,process.returncode,node,mutation['path'],pin)
                outcomes.append(dict(label=label,ok=ok,process_exit=process.returncode,receipt=name+'.json',source_sha256=pin))
        finally:
            product.write_bytes(original)
        rows.append(dict(contract=check,outcomes=outcomes,source_restored=product.read_bytes()==original,
            ok=len(outcomes)==2 and all(row['ok'] for row in outcomes) and product.read_bytes()==original))
    return rows


def write(path, packet):
    path.write_text(json.dumps(packet, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inventory-only', action='store_true')
    parser.add_argument('--force-skip', action='store_true', help='deliberately RED hosted control')
    parser.add_argument('--base-ref',default=ACCEPTED_BASE,help='independently supplied accepted Git base SHA')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        base=base_manifest_at(ROOT,args.base_ref)
        checked = inventory(ROOT, json.loads(MANIFEST.read_text()),base_manifest=base)
    except ValueError as error:
        checked=dict(ok=False,invalid=[str(error)],transitions=[])
    packet = dict(inventory=checked, suites=[], gate_exit=1,
                  manifest_sha256=hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
                  python=sys.version, qualification='HOSTED_OFFLINE_SOURCE_CONTRACTS_ONLY',
                  native_qualified=False, installed_qualified=False, old_installation='G_OLD_UNRESOLVED',
                  base_ref=args.base_ref,
                  policy_base_ref=ACCEPTED_BASE if args.base_ref==BOOTSTRAP_MAIN else args.base_ref,
                  control='forced-skip' if args.force_skip else 'normal')
    result = args.output / 'summary.json'
    write(result, packet)
    print(json.dumps(checked, indent=2), flush=True)
    if not checked['ok']:
        print('NEXT_OFFLINE_INVENTORY_RED', flush=True)
        return 1
    if args.inventory_only:
        packet['gate_exit'] = 0
        write(result, packet)
        print('NEXT_OFFLINE_INVENTORY_GREEN', flush=True)
        return 0
    denied = []
    for family in (socket.AF_INET, socket.AF_INET6):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            try:
                probe = socket.socket(family, kind)
            except OSError as error:
                denied.append(dict(family=int(family), kind=int(kind), errno=error.errno,
                                   denied=error.errno == errno.EAFNOSUPPORT))
            else:
                probe.close()
                denied.append(dict(family=int(family), kind=int(kind), denied=False))
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    packet['offline_runtime'] = dict(socket_creation_probes=denied,
        network_namespace=os.readlink('/proc/self/ns/net'), uid=os.getuid(),
        no_new_privs=status['NoNewPrivs'].strip(), ip_socket_denied=all(r['denied'] for r in denied))
    write(result, packet)
    if not packet['offline_runtime']['ip_socket_denied'] or packet['offline_runtime']['no_new_privs'] != '1':
        print('NEXT_OFFLINE_RUNTIME_RESTRICTION_RED', flush=True)
        return 1
    import psycopg
    packet['source_head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    packet['dependencies'] = {name: importlib.metadata.version(name) for name in
                              ('pytest', 'numpy', 'psycopg', 'jsonschema', 'asyncpg', 'httpx', 'starlette')}
    packet['driver'] = dict(version=psycopg.__version__, pq_impl=psycopg.pq.__impl__, libpq_version=psycopg.pq.version())
    packet['unchanged_u13_validator_sha256'] = hashlib.sha256(
        Path(__file__).with_name('pytest_u13_worker.py').read_bytes()).hexdigest()
    write(result, packet)
    worker = Path(__file__).with_name('pytest_next_worker.py')
    with tempfile.TemporaryDirectory(prefix='next-offline-') as temporary:
        case = Path(temporary)
        case.chmod(0o700)
        # Child tests receive no inherited tokens, credentials or database endpoints.
        environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
        environment.update(HOME=str(case / 'home'), TMPDIR=str(case),
                           CORTEX_U13_CASE_ROOT=str(case), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                           PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        (case / 'home').mkdir()
        proofs=retirement_proofs(ROOT,checked['retirement_checks'],args.output/'retirement-proofs',case,environment)
        packet['retirement_proofs']=proofs;write(result,packet)
        if not all(row['ok'] for row in proofs):
            print('NEXT_OFFLINE_RETIREMENT_PROOF_RED',flush=True)
            return 1
        for index, suite in enumerate(checked['run']):
            retired = [row for row in checked['retired'] if row['nodeid'].split('::')[0] == suite]
            excluded=[row for row in checked['method_exclusions'] if row['nodeid'].split('::')[0]==suite]
            name = f'{index:03d}'
            destination = args.output / (name + '.json')
            command = [sys.executable, '-B', str(worker), '--suite', suite,
                       '--output', str(destination), '--temporary', str(case / name),
                       '--retired-json', json.dumps(retired)]
            command+=['--excluded-json',json.dumps(excluded)]
            source_sha = hashlib.sha256((ROOT / suite).read_bytes()).hexdigest()
            if args.force_skip and index == 0:
                command.append('--force-skip')
            started = time.monotonic()
            try:
                process = subprocess.run(command, cwd=ROOT, env=environment,
                                         capture_output=True, text=True, timeout=180)
                code, log = process.returncode, process.stdout + process.stderr
            except subprocess.TimeoutExpired as error:
                code, log = 124, 'SUITE_TIMEOUT\n' + str(error.stdout) + str(error.stderr)
            (args.output / (name + '.txt')).write_text(log)
            try:
                outcome = json.loads(destination.read_text())
            except (OSError, ValueError):
                outcome = None
            source_unchanged = hashlib.sha256((ROOT / suite).read_bytes()).hexdigest() == source_sha
            ok = (receipt_valid(outcome, code, retired=retired,
                                replacements=checked['replacement_nodes'].get(suite, ()),excluded=excluded) and source_unchanged
                  and outcome.get('suite') == suite and outcome.get('source_sha256') == source_sha
                  and all(node.startswith(suite + '::') for node in outcome['selected']))
            row = dict(suite=suite, source_sha256=source_sha, source_unchanged=source_unchanged,
                       process_exit=code, receipt=name + '.json', log=name + '.txt',
                       elapsed_seconds=round(time.monotonic() - started, 3), ok=ok,
                       selected=len(outcome.get('selected', [])) if outcome else 0,
                       live_collected=len([n for n in outcome.get('selected',[]) if n not in {e['nodeid'] for e in excluded if e['evidence']['kind']=='unbuilt_unit'}]) if outcome else 0,
                       declared_unavailable=len([e for e in excluded if e['evidence']['kind']=='unbuilt_unit']),
                       passed=sum(r['nodeid'] not in {e['nodeid'] for e in excluded if e['evidence']['kind']=='unbuilt_unit'} and r['when'] == 'call' and r['outcome'] == 'passed'
                                  and not r['wasxfail'] for r in outcome.get('reports', [])) if outcome else 0)
            packet['suites'].append(row)
            write(result, packet)
            print(f"{suite}: {'GREEN' if ok else 'RED'} selected={row['selected']} passed={row['passed']} exit={code}", flush=True)
    packet.update(owned_case_root_absent=not case.exists(),
                  suites_run=len(packet['suites']), suites_passed=sum(row['ok'] for row in packet['suites']),
                  tests_selected=sum(row['selected'] for row in packet['suites']),
                  tests_live_collected=sum(row['live_collected'] for row in packet['suites']),
                  tests_declared_unavailable=sum(row['declared_unavailable'] for row in packet['suites']),
                  tests_passed=sum(row['passed'] for row in packet['suites']))
    packet['gate_exit'] = 0 if (packet['owned_case_root_absent'] and
        len(packet['suites']) == len(checked['run']) and all(row['ok'] for row in packet['suites'])) else 1
    write(result, packet)
    print('NEXT_OFFLINE_GREEN' if packet['gate_exit'] == 0 else 'NEXT_OFFLINE_RED', flush=True)
    return packet['gate_exit']


if __name__ == '__main__':
    raise SystemExit(main())
