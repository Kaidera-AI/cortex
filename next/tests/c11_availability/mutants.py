"""Source-copy fault operators for the C11 real-PG outage test."""

import hashlib
import json
from pathlib import Path
import sys


SOURCE = Path('/tmp/next/src/cortex_core/api/c11a.py')
MUTANTS = {
    'probe_bypass': [(
        'return await asyncio.wait_for(asyncio.shield(current), timeout_seconds)',
        'return True',
    )],
    'cached_ready': [(
        'if flight is None or flight.done():', 'if flight is None:',
    ), (
        'flight.add_done_callback(discard)', 'pass',
    )],
    'success_shaped_outage': [(
        'if core is not True:\n            return await _respond(send, 503, _packet("core_unavailable"))',
        "if core is not True:\n            return await _respond(send, 200, {'results': []})",
    )],
    'false_committed_write': [(
        "            status, packet = self._d1_refusal(error) if d2 else _error_status(error)\n"
        "            return await _respond(send, status, packet)",
        "            if row['effect'] == 'write':\n"
        "                return await _respond(send, 200, {'receipt': {'event_id': 'false'}})\n"
        "            status, packet = self._d1_refusal(error) if d2 else _error_status(error)\n"
        "            return await _respond(send, status, packet)",
    )],
    'remove_single_flight': [(
        'if flight is None or flight.done():', 'if True:',
    )],
    'remove_factory_deadline': [(
        'connect_timeout_seconds=timeout_seconds,\n'
        '                statement_timeout_ms=max(1, int(timeout_seconds * 1000))',
        'connect_timeout_seconds=1000,\n'
        '                statement_timeout_ms=1000000',
    )],
}


def main():
    name = sys.argv[1]
    if name not in MUTANTS:
        raise ValueError('unknown source-copy mutant')
    body = SOURCE.read_text()
    for before, after in MUTANTS[name]:
        if body.count(before) != 1:
            raise ValueError('source-copy mutation anchor is not unique')
        body = body.replace(before, after)
    SOURCE.write_text(body)
    print(json.dumps({'mutant': name, 'file': str(SOURCE),
                      'source_copy_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
