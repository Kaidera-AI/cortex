"""Stdlib AST inspection of controller suite selection; no controller execution."""
import ast
import hashlib
import json
from pathlib import Path
import sys

p = Path(sys.argv[1])
phase = sys.argv[2]
selected = set()

def scan(node):
    if isinstance(node, ast.If) and isinstance(node.test, ast.Call):
        call = node.test
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id == 'PHASE':
            assert call.func.attr == 'startswith' and len(call.args) == 1
            for child in node.body if phase.startswith(ast.literal_eval(call.args[0])) else node.orelse:
                scan(child)
            return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('run', 'checked'):
        if node.args and isinstance(node.args[0], ast.BinOp) and isinstance(node.args[0].right, ast.List):
            args = node.args[0].right.elts
            if args and isinstance(args[-1], ast.Constant) and isinstance(args[-1].value, str):
                value = args[-1].value
                if value.startswith('/tmp/next/tests/') and value != '/tmp/next/tests/test_receipts.py':
                    selected.add(value.rsplit('/', 1)[1])
    for child in ast.iter_child_nodes(node):
        scan(child)

scan(ast.parse(p.read_bytes()))
required = {'schema', 'auth', 'records', 'coordination', 'core_adapters', 'acceptance_guards', 'auth_identity'}
missing = required - selected
print(json.dumps({'controller_sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                  'phase': phase, 'selected': sorted(selected), 'missing': sorted(missing)}))
assert not missing, ('missing_predecessor_suite', sorted(missing))
