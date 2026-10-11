"""Prove Gate A calls the shared public classifier, with actual extraction."""
import base64
import hashlib
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import keys


class SharedKeysCase(unittest.TestCase):
    def test_shared_source_and_actual_classifier_call(self):
        source = ROOT / 'console_keys.py'
        self.assertTrue(source.is_file(), 'shared classifier module is absent')
        donor = Path('/Volumes/WD-B-4TB/DevVault/helix/output/KOS/bob-native-linux-plan-rework-20261011/kos-layer/console_keys.py')
        self.assertEqual(source.read_bytes(), donor.read_bytes())
        shared = importlib.import_module('console_keys')
        with patch.object(shared, 'classify_console_keys', wraps=shared.classify_console_keys) as classify:
            outcome = keys.decoded_outcome(base64.b64encode(b'ssh-ed25519 AAAA one\nssh-ed25519 AAAA duplicate\n').decode(), 'base64')
        self.assertEqual(outcome['exit'], 0)
        classify.assert_called_once_with(['AAAA', 'AAAA'])

    def test_actual_caller_waits_only_zero_and_retires_conflict_decode(self):
        caller = getattr(keys, 'bind_console', None)
        self.assertTrue(callable(caller), 'bounded console caller is absent')
        for raws, expected, waits, retired in [(['', 'ssh-ed25519 AAAA one\n'], 0, [60], []),
                (['ssh-ed25519 AAAA a\nssh-ed25519 BBBB b\n'], 76, [], ['retire']),
                (['invalid base64 !'], 76, [], ['retire'])]:
            with self.subTest(expected=expected, raw=raws):
                clock = [0]; sleeps = []; retire = []; iterator = iter(raws)
                def sleep(seconds):
                    sleeps.append(seconds); clock[0] += seconds
                result = caller(lambda: next(iterator), lambda: retire.append('retire'),
                                lambda: clock[0], sleep, watchdog_armed=True)
                self.assertEqual(result['exit'], expected)
                self.assertEqual(sleeps, waits)
                self.assertEqual(retire, retired)

    def test_actual_caller_deadline_stops_and_retires(self):
        caller = getattr(keys, 'bind_console', None)
        self.assertTrue(callable(caller), 'bounded console caller is absent')
        clock = [0]; retire = []; calls = []
        def sleep(seconds): clock[0] += seconds
        result = caller(lambda: calls.append(clock[0]) or '', lambda: retire.append('retire'),
                        lambda: clock[0], sleep, watchdog_armed=True)
        self.assertEqual(result['exit'], 75)
        self.assertEqual(calls, list(range(0, 600, 60)))
        self.assertEqual(retire, ['retire'])


if __name__ == '__main__': unittest.main(verbosity=2)
