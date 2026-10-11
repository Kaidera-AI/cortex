"""Source-copy fault operators for the C11 real-PG outage test."""

import hashlib
import json
from pathlib import Path
import sys


SOURCE = Path('/tmp/next/src/cortex_core/api/c11a.py')
MUTANTS = {
    'probe_bypass': (
        'return await asyncio.wait_for(asyncio.to_thread(check), timeout_seconds)',
        'return True',
    ),
    'cached_ready': (
        '    async def probe():\n        try:\n'
        '            return await asyncio.wait_for(asyncio.to_thread(check), timeout_seconds)',
        '    cached = [None]\n    async def probe():\n'
        '        if cached[0] is not None:\n            return cached[0]\n'
        '        try:\n            cached[0] = await asyncio.wait_for('
        'asyncio.to_thread(check), timeout_seconds)\n            return cached[0]',
    ),
    'success_shaped_outage': (
        'if core is not True:\n            return await _respond(send, 503, _packet("core_unavailable"))',
        "if core is not True:\n            return await _respond(send, 200, {'results': []})",
    ),
    'false_committed_write': (
        "            status, packet = self._d1_refusal(error) if d2 else _error_status(error)\n"
        "            return await _respond(send, status, packet)",
        "            if row['effect'] == 'write':\n"
        "                return await _respond(send, 200, {'receipt': {'event_id': 'false'}})\n"
        "            status, packet = self._d1_refusal(error) if d2 else _error_status(error)\n"
        "            return await _respond(send, status, packet)",
    ),
}


def main():
    name = sys.argv[1]
    if name not in MUTANTS:
        raise ValueError('unknown source-copy mutant')
    before, after = MUTANTS[name]
    body = SOURCE.read_text()
    if body.count(before) != 1:
        raise ValueError('source-copy mutation anchor is not unique')
    SOURCE.write_text(body.replace(before, after))
    print(json.dumps({'mutant': name, 'file': str(SOURCE),
                      'source_copy_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
