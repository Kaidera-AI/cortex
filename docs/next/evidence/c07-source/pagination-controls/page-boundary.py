"""Original reviewer model, excluding only four top-level RED diagnostics.
The unchanged test_held_continuation.py evaluates its original target/cursor contract.
No model or consumer branch is changed by this adapter.
"""
import ast
from pathlib import Path
original=Path(__file__).with_name('page-boundary-red-original.py')
tree=ast.parse(original.read_text())
assert sum(isinstance(n,ast.Assert) for n in tree.body)==4
tree.body=[n for n in tree.body if not isinstance(n,ast.Assert)]
exec(compile(tree,str(original),'exec'),globals())
