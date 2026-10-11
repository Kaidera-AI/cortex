"""Reviewer RED probes of exact AST branches, with in-memory doubles only.

No product import, DB, container or source writes. These are not native PG proof.
Run: python3 test_c07_repair_manifest.py /path/to/review-checkout
"""
import ast
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Protocol
import unittest
from uuid import UUID

ROOT = Path(sys.argv[1])
CONSUMER = ROOT / 'next/src/cortex_core/modules/consumer.py'
PROTOCOL = ROOT / 'next/src/cortex_core/modules/protocol.py'


class ConsumerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


namespace = dict(UUID=UUID, hashlib=hashlib, re=re, ConsumerError=ConsumerError,
                 OutboxError=type('OutboxError', (Exception,), {}))
tree = ast.parse(CONSUMER.read_text())
klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ModuleConsumer')
for method in klass.body:
    if isinstance(method, ast.FunctionDef) and method.name in ('_validate', '_apply_event', 'repair'):
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<exact-C07-method>', 'exec'), namespace)
manifest_namespace = dict(dataclass=dataclass, Protocol=Protocol)
exec(compile(ast.Module(body=[n for n in ast.parse(PROTOCOL.read_text()).body
                              if isinstance(n, ast.ClassDef)], type_ignores=[]),
             '<exact-C07-manifest>', 'exec'), manifest_namespace)


class Harness:
    _validate = namespace['_validate']
    _apply_event = namespace['_apply_event']
    repair = namespace['repair']
    installation_id = UUID(int=1)
    project_id = UUID(int=3)
    manifest = SimpleNamespace(schema_version=1, aggregate_kinds=('memory',))
    after_checkpoint = None

    def __init__(self, events, sink):
        self.feed = SimpleNamespace(page=lambda **kw: SimpleNamespace(events=events))
        self.sink = sink
        self.log = []

    def hold_lock(self):
        return nullcontext()

    def _state(self):
        return dict(generation=1, applied_cursor=0)

    def status(self):
        return dict(log=self.log)

    def _call(self, *args):
        return True  # the conflicting aggregate still has unresolved poison

    def _record(self, state, event, outcome, error=None, receipt=None):
        self.log.append((event.cursor, outcome, error, receipt))


def event(cursor, aggregate=50, revision=1):
    body = b'valid'
    return SimpleNamespace(cursor=cursor, payload=body, envelope=dict(
        installation_id=str(UUID(int=1)), project_id=str(UUID(int=3)),
        aggregate_id=str(UUID(int=aggregate)), event_id=str(UUID(int=100 + cursor)),
        aggregate_kind='memory', schema_version=1, aggregate_revision=revision,
        operation='upsert', tombstone=False, payload_sha256=hashlib.sha256(body).hexdigest()))


class Sink:
    def __init__(self, conflicting_cursor=None):
        self.conflicting_cursor = conflicting_cursor
        self.calls = []

    def apply(self, value, generation):
        self.calls.append(value.cursor)
        if value.cursor == self.conflicting_cursor:
            raise ValueError('target_conflict')
        e = value.envelope
        return dict(event_id=e['event_id'], aggregate_id=e['aggregate_id'],
                    revision=e['aggregate_revision'], tombstone=e['tombstone'],
                    payload_sha256=e['payload_sha256'], generation=generation, stale=False)


class ReviewRed(unittest.TestCase):
    def test_unresolved_conflict_does_not_apply_held_successor(self):
        first, successor = event(1), event(2, revision=2)
        sink = Sink(conflicting_cursor=1)
        harness = Harness((first, successor), sink)
        try:
            harness.repair(UUID(first.envelope['event_id']))
        except ConsumerError:
            pass
        self.assertEqual(sink.calls, [1], 'unresolved poison must still fence its successor')

    def test_unrelated_poison_does_not_block_requested_aggregate_repair(self):
        other, requested = event(1), event(2, aggregate=51)
        other.payload = b'corrupted'
        sink = Sink()
        harness = Harness((other, requested), sink)
        try:
            harness.repair(UUID(requested.envelope['event_id']))
        except ConsumerError as error:
            self.fail('unrelated aggregate blocked requested repair: ' + error.code)
        self.assertEqual(sink.calls, [2])

    def test_manifest_refuses_shapes_outside_published_schema(self):
        for case in (dict(protocol_version=True, schema_version=True),
                     dict(aggregate_kinds=['memory', 'memory']),
                     dict(aggregate_kinds=['x' * 65])):
            with self.subTest(case=case):
                value = dict(module_id='graph', protocol_version=1, schema_version=1,
                             aggregate_kinds=['memory'], sink='postgres')
                value.update(case)
                with self.assertRaises(ValueError):
                    manifest_namespace['ModuleManifest'].parse(value)


if __name__ == '__main__':
    print(json.dumps(dict(head=subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                                       text=True).strip(),
                          source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in (CONSUMER, PROTOCOL)})), flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReviewRed))
    print(json.dumps(dict(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors))), flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
