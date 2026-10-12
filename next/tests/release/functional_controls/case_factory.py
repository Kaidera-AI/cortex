"""Bounded stdlib-only reviewer fixture producer; no product imports/effects.

The public fixture producer's definitions are source-hash-bound and AST copied.
One declared reviewer hook before input materialization creates coherent public
fixtures; all original profile/input/artifact/report/custody construction stays
in that producer. Opaque bytes are never executed. This is not an oracle.
Root may feed case(label) into unchanged matrix.api/matrix.cli under its guards.
"""
import ast
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile

HERE = Path(__file__).resolve().parent
BINDING = json.loads((HERE/'bindings.json').read_bytes())
ROOT = Path(BINDING['source_root'])


def source(relative):
    row = next(row for row in BINDING['files'] if row['path'] == relative)
    raw = (ROOT/relative).read_bytes()
    assert len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256']
    return raw


def load_defs(relative, names, namespace, *, hook=False):
    tree = ast.parse(source(relative))
    nodes = [copy.deepcopy(node) for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    if hook:
        family = next(node for node in nodes if node.name == 'family')
        assignments = [i for i,node in enumerate(family.body) if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'additions' for t in node.targets)]
        assert len(assignments) == 1
        insertion = ast.parse('_review_hook(value, inputs, base, first, second, runtime, profile)').body[0]
        original_dump = ast.dump(family, include_attributes=False)
        family.body.insert(assignments[0], insertion)
        restored = copy.deepcopy(family)
        restored.body.pop(assignments[0])
        assert ast.dump(restored, include_attributes=False) == original_dump
    compiled = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(compiled, relative, 'exec'), namespace)


def case(label='baseline'):
    assert label in {'baseline','whiteout_cmd_input','cmd_unreadable','cmd_parent_unsearchable'}
    namespace = dict(copy=copy,io=io,json=json,tarfile=tarfile,hashlib=hashlib,
                     INDEX='application/vnd.oci.image.index.v1+json',
                     MANIFEST='application/vnd.oci.image.manifest.v1+json',
                     CONFIG='application/vnd.oci.image.config.v1+json',
                     LAYER='application/vnd.oci.image.layer.v1.tar',
                     CLASSES=['accounts','cache','clock','files','input','labels','logs',
                              'python','runtime','serialization','xattrs-acls'],
                     PHASES=['input','parser','build','export','audit','cleanup'])
    load_defs('next/tests/release/fixture.py', ['canonical','digest','file','build_input'], namespace)
    load_defs('next/tests/release/receipt_fixture.py', ['sha','tar'], namespace)
    load_defs('next/tests/release/native_matrix_fixture.py', ['row','inventory','pack','desc'], namespace)

    def hook(value, inputs, base, first, second, runtime, profile):
        inventory = namespace['inventory']
        if label == 'whiteout_cmd_input':
            # OCI regular-file whiteout deletes the required /app/config.json.
            # Approved profile still declares whiteouts='deny', so accepting
            # this coherent syntactic policy must not waive that invariant.
            second.append(namespace['row']('app/.wh.config.json', b''))
            profile['layer_policy']['generated']['two'] = inventory(second)
            profile['layer_policy']['member_order']['two'] = [r['path'] for r in second]
        elif label == 'cmd_unreadable':
            config = next(r for r in first if r['path'] == 'app/config.json')
            config.update(uid=0, gid=0, mode=0o600)
            profile['layer_policy']['generated']['one'] = inventory(first)
        elif label == 'cmd_parent_unsearchable':
            # Select the already accepted root-owned0755 base program, and move
            # WorkingDir to the already admitted /usr/lib. Only the Cmd config
            # footprint traverses /app after this coherent profile amendment.
            directory = next(r for r in first if r['path'] == 'app')
            directory.update(uid=2002, gid=2002, mode=0o700)
            profile['layer_policy']['generated']['one'] = inventory(first)
            profile['layer_policy']['transitions'][0]['after'] = inventory([directory])[0]
            runtime['WorkingDir'] = '/usr/lib'
            profile['oci_config']['WorkingDir'] = '/usr/lib'
            profile['runtime']['working_dir'] = '/usr/lib'

    namespace['_review_hook'] = hook
    load_defs('next/tests/release/native_matrix_fixture.py', ['family'], namespace, hook=True)
    return namespace['family'](fault='base_runtime_selected' if label == 'cmd_parent_unsearchable' else None)
