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
comparison=Path(__file__).with_name('pre-matrix-quarantine-fixture-original.py')
assertions=lambda source:[ast.dump(n,include_attributes=False) for n in ast.walk(ast.parse(source)) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr.startswith('assert')]
quarantine=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='test_unresolved_quarantine_is_not_silently_pruned')
event_assignments=[n for n in ast.walk(quarantine) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='event' for t in n.targets)]
canonical=bool(event_assignments) and "'event_id'" in ast.unparse(event_assignments[0].value) and 'p.publish()' in ast.unparse(event_assignments[0].value)
checks['matrix_repair_fixture']={'all_assertions_preserved':assertions(p.read_text())==assertions(comparison.read_text()),'canonical_published_quarantine_target':canonical}
passed=all(all(v.values()) for v in checks.values())
print(json.dumps({'passed':passed,'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'checks':checks}))
raise SystemExit(0 if passed else 1)
