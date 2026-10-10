"""Stdlib source fixture contract; no product import/test/build on the host."""
import ast
from pathlib import Path
import sys
root = Path.cwd()
test = Path(sys.argv[1]) if len(sys.argv)>1 else root/'packages/containers/graph-worker/tests/test_builder_pyc.py'
tree = ast.parse(test.read_text())
assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t,ast.Name) and t.id=='builder' for t in n.targets))
value = assignment.value
assert isinstance(value,ast.Subscript) and isinstance(value.value,ast.Call)
call = value.value
assert isinstance(call.func,ast.Attribute) and isinstance(call.func.value,ast.Name) and call.func.value.id=='source' and call.func.attr=='split'
args = [ast.literal_eval(x) for x in call.args]
index = ast.literal_eval(value.slice)
source = (root/'packages/containers/graph-worker/Dockerfile').read_text().replace('\\\n','')
builder = source.split(*args)[index]
runs = [line[4:] for line in builder.splitlines() if line.startswith('RUN ') and '/build/fetch-qwen3-model.py' in line]
assert len(runs)==1, 'fixture must reach the actual builder fetch RUN before native execution'
assert '--no-compile' in builder, 'approved explicit compiler mechanism requires pip no-compile'
print('fixture parser reaches one real builder RUN; pip no-compile preserved')
