"""AST-only expiry seed constraint guard; never imports product code."""
import ast,hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]);tree=ast.parse(p.read_text())
f=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='test_expired_checkpoint_cannot_advance_or_reactivate_completeness')
sql=next(n.value for n in ast.walk(f) if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'INSERT INTO coordination.consumer_checkpoints' in n.value)
checks={'last_seen_column': 'last_seen_at' in sql, 'expiry_column': 'expires_at' in sql, 'last_seen_before_expiry': "interval '2 hours'" in sql and "interval '1 hour'" in sql}
passed=all(checks.values());print(json.dumps({'passed':passed,'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'checks':checks}));raise SystemExit(0 if passed else 1)
