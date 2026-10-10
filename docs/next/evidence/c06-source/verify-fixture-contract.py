"""Stdlib AST contract guard: C04 wraps SQL failures before a caller sees them."""
import ast
import hashlib
import json
from pathlib import Path
import sys
p=Path(sys.argv[1]); tree=ast.parse(p.read_text())
targets=['test_raw_bound_job_write_without_parent_capture_refuses_commit','test_reserved_alias_and_fact_names_cannot_be_forged_by_writer']
checks={}
for name in targets:
 f=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
 raises=[n for n in ast.walk(f) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='assertRaises']
 first=raises[0]
 typed=bool(first.args and isinstance(first.args[0],ast.Name) and first.args[0].id=='AuthError')
 code=any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='assertEqual'
          and any(isinstance(a,ast.Constant) and a.value=='core_unavailable' for a in n.args)
          and any(isinstance(a,ast.Attribute) and a.attr=='code' for a in n.args) for n in ast.walk(f))
 checks[name]={'typed_sql_refusal':typed,'fixed_code_asserted':code}
passed=all(all(v.values()) for v in checks.values())
print(json.dumps({'passed':passed,'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'checks':checks}))
raise SystemExit(0 if passed else 1)
