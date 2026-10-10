"""Stdlib AST custody guard for C06 fixture use of the existing identity contract."""
import ast
import json
from pathlib import Path
import sys

root=Path(sys.argv[1])
identity=ast.parse((root/'next/src/cortex_core/identity.py').read_bytes())
pin=next(n for n in identity.body if isinstance(n,ast.ClassDef) and n.name=='DonorPin')
fields=[n.target.id for n in pin.body if isinstance(n,ast.AnnAssign)]
authority=ast.literal_eval(next(n.value for n in identity.body if isinstance(n,ast.Assign)
    and any(isinstance(t,ast.Name) and t.id=='_AUTHORITY' for t in n.targets)))
fixture=ast.parse((root/'next/tests/outbox/test_outbox.py').read_bytes())
pins=0; manifests=0
for n in ast.walk(fixture):
    if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='DonorPin':
        assert len(n.args)<=len(fields), 'DonorPin fixture exceeds current constructor arity'
        assert all(k.arg in fields for k in n.keywords), 'unknown DonorPin field'
        pins+=1
    if isinstance(n,ast.Dict):
        for k,v in zip(n.keys,n.values):
            if isinstance(k,ast.Constant) and k.value=='legacy_registration_authority':
                assert ast.literal_eval(v)==authority, 'manifest must preserve current owner/admin authority contract'
                manifests+=1
assert pins==1 and manifests==1
print(json.dumps({'identity_constructor_fields':fields,'DonorPin_fixtures':pins,'authority_manifests':manifests,'matched':True}))
