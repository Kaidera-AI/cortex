"""Source-copy mutants for the current-manifest staged migration boundary."""

import hashlib
import json
from pathlib import Path
import sys


SOURCE = Path('/tmp/next/src/cortex_core/migrations.py')
MUTANTS = {
    'ignored_through': (
        'for identity, digest, sql in prepared[:prepared_target + 1]:',
        'for identity, digest, sql in prepared:',
    ),
    'missing_prefix_check': (
        'if ledger_ids != {item[0] for item in prepared[:len(ledger_ids)]}:',
        'if False:',
    ),
    'unknown_target_accepted': (
        'if type(through) is not str or through not in seen:\n'
        '            raise MigrationError("Unknown migration target")',
        'if type(through) is not str or through not in seen:\n'
        '            through = prepared[-1][0]',
    ),
    'duplicate_execution_after_reconnect': (
        'if identity in ledger:\n                continue',
        'if identity in ledger and identity != through:\n                continue',
    ),
}


def main():
    name = sys.argv[1]
    if name not in MUTANTS:
        raise ValueError('unknown migration-head mutant')
    before, after = MUTANTS[name]
    source = SOURCE.read_text()
    if source.count(before) != 1:
        raise ValueError('migration-head mutation anchor is not unique')
    SOURCE.write_text(source.replace(before, after))
    print(json.dumps({'mutant': name, 'file': str(SOURCE),
                      'source_copy_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
