"""Separate, one-shot legacy R121 no-count-ceiling receipt."""

import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'c11b2'))
from core_db_fixture import uid


class AllIdsScale(unittest.TestCase):
    def test_every_committed_id_is_returned_beyond_ten_thousand(self):
        from test_session_ids_read import SessionIdsRead
        fixture = SessionIdsRead('test_request_role_cannot_select_private_session_sources')
        fixture.setUp()
        try:
            for number in range(1000, 11001):
                fixture.seed(number)
            began = time.monotonic()
            status, body = fixture.call()
            elapsed_ms = round((time.monotonic() - began) * 1000, 3)
            self.assertEqual(status, 200, body if status != 200 else None)
            self.assertEqual(body['project'], '3')
            self.assertEqual(len(body['ids']), 10001)
            self.assertEqual(body['ids'], [uid(number) for number in range(1000, 11001)])
            print('C11_IDS_SCALE=' + json.dumps({
                'ids': len(body['ids']), 'response_bytes': len(json.dumps(body).encode()),
                'get_ms': elapsed_ms}, sort_keys=True), flush=True)
        finally:
            fixture.doCleanups()
