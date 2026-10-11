"""Source-copy mutants for O01 metadata identity validation."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
MUTATIONS = {
    'schema-ledger-bypass': ('backup.py', 'if key in seen:',
                             'if key in seen and label != "schema ledger":'),
    'model-bypass': ('backup.py', 'if key in seen:',
                     'if key in seen and label != "model":'),
    'physical-blob-bypass': ('backup.py', 'if key in seen:',
                             'if key in seen and label != "physical blob member":'),
    'scope-vector-bypass': ('backup_validation.py',
                            'if any((*_metadata_scope(row), _uuid_identity(row["record_id"]),',
                            'if False and any((*_metadata_scope(row), _uuid_identity(row["record_id"]),'),
    'raw-blob-member-bypass': ('backup.py',
                               'lambda row: _blob_member(row["object_key"]), "physical blob member")',
                               'lambda row: row["object_key"], "physical blob member")'),
    'raw-uuid-model-bypass': ('backup.py',
                              'lambda row: (*_metadata_scope(row), _uuid_identity(row["id"])),\n                     "model")',
                              'lambda row: (*_metadata_scope(row), row["id"]),\n                     "model")'),
}


def main():
    name = sys.argv[1] if len(sys.argv) == 2 else None
    if name not in MUTATIONS:
        raise ValueError('unknown source-copy mutation')
    with tempfile.TemporaryDirectory(prefix='cox-o01-metadata-mutant-') as dirname:
        source = Path(dirname)/'src'
        shutil.copytree(ROOT/'src', source)
        filename, before, after = MUTATIONS[name]
        path = source/'cortex_core'/filename
        body = path.read_text()
        if body.count(before) != 1:
            raise RuntimeError('mutation anchor differs')
        path.write_text(body.replace(before, after, 1))
        env = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run(
            [sys.executable, str(ROOT/'tests/test_receipts.py'),
             str(ROOT/'tests/backup_validation')],
            text=True, capture_output=True, env=env, timeout=180)
        print(json.dumps({
            'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                    text=True).strip(),
            'mutation': name,
            'mutant_source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'exit_code': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr,
        }, sort_keys=True))
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
