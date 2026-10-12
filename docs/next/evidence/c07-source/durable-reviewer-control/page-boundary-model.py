"""Exact consumer AST replay with an in-memory model of the SQL ledger.

This proves source flow only, not PostgreSQL behavior or native qualification.
Run: python3 page-boundary.py /tmp/cortex-mike-pr71-c07-rereview-20261011
"""
import ast
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

root = Path(sys.argv[1])
path = root / 'next/src/cortex_core/modules/consumer.py'
source = path.read_text()
tree = ast.parse(source)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ModuleConsumer')
methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
           and n.name in ('_validate', '_apply_event', 'repair', 'cycle')]


class ConsumerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


ns = {'UUID': UUID, 'hashlib': hashlib, 're': re, 'ConsumerError': ConsumerError,
      'OutboxError': type('OutboxError', (Exception,), {})}
for method in methods:
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<exact-consumer-method>', 'exec'), ns)


def event(cursor, aggregate, revision=1):
    body = ('event-' + str(cursor)).encode()
    return SimpleNamespace(cursor=cursor, payload=body, envelope=dict(
        installation_id=str(UUID(int=1)), project_id=str(UUID(int=3)),
        aggregate_id=str(UUID(int=aggregate)), event_id=str(UUID(int=2000 + cursor)),
        aggregate_kind='memory', schema_version=1, aggregate_revision=revision,
        operation='upsert', tombstone=False, payload_sha256=hashlib.sha256(body).hexdigest()))


class Harness:
    _validate = ns['_validate']
    _apply_event = ns['_apply_event']
    repair = ns['repair']
    cycle = ns['cycle']
    installation_id = UUID(int=1)
    project_id = UUID(int=3)
    module_id = 'graph'
    manifest = SimpleNamespace(schema_version=1, aggregate_kinds=('memory',))
    after_checkpoint = None
    max_pending_outcomes = 10000

    def __init__(self, events):
        self.events = events
        self.state = dict(generation=1, applied_cursor=0, scan_cursor=1001, state='active', floor=0)
        self.outcomes = {e.cursor: ('poison' if e.cursor == 1 else 'held' if e.cursor == 1001 else 'applied')
                         for e in events}
        self.quarantined = {events[0].envelope['event_id']}
        self.target = {}
        self.calls = []
        self.feed = SimpleNamespace(page=self.page)
        self.sink = SimpleNamespace(apply=self.apply)

    def page(self, after, limit):
        return SimpleNamespace(events=tuple(e for e in self.events if e.cursor > after)[:limit],
                               head=self.events[-1].cursor, floor=0)

    def hold_lock(self):
        return nullcontext()

    def _state(self):
        return dict(self.state)

    def status(self):
        return {**self.state, 'pending_outcomes': len(self.outcomes),
                'outcomes': dict(self.outcomes), 'target': dict(self.target), 'sink_calls': list(self.calls)}

    def apply(self, e, generation):
        self.calls.append(e.cursor)
        v = e.envelope
        self.target[v['aggregate_id']] = v['aggregate_revision']
        return dict(event_id=v['event_id'], aggregate_id=v['aggregate_id'], revision=v['aggregate_revision'],
                    tombstone=v['tombstone'], payload_sha256=v['payload_sha256'], generation=generation, stale=False)

    def _call(self, signature, args):
        if signature.startswith('c07_quarantined'):
            return str(args[-1]) in self.quarantined
        if signature.startswith('c07_poisoned'):
            aggregate = str(args[-1])
            return any(e.envelope['aggregate_id'] == aggregate and e.envelope['event_id'] in self.quarantined
                       for e in self.events)
        if signature.startswith('c07_scan'):
            self.state['scan_cursor'] = args[-1]
            return self.status()
        raise AssertionError(signature)

    def _record(self, state, e, outcome, error=None, receipt=None):
        self.outcomes[e.cursor] = outcome
        if outcome == 'poison':
            self.quarantined.add(e.envelope['event_id'])
        elif outcome in ('applied', 'stale'):
            self.quarantined.discard(e.envelope['event_id'])
        self.state['scan_cursor'] = max(self.state['scan_cursor'], e.cursor)
        # Mirror c07_record's ordered contiguous project-outcome fold and
        # completed-prefix DELETE; no unproved database calls are simulated.
        applied = self.state['applied_cursor']
        for candidate in self.events:
            if candidate.cursor <= applied:
                continue
            if candidate.cursor > self.state['scan_cursor']:
                break
            if self.outcomes.get(candidate.cursor, 'missing') not in ('applied', 'stale'):
                break
            applied = candidate.cursor
        self.state['applied_cursor'] = applied
        self.outcomes = {cursor: result for cursor, result in self.outcomes.items()
                         if not (cursor <= applied and result in ('applied', 'stale'))}


events = [event(1, 50)] + [event(cursor, 10000 + cursor) for cursor in range(2, 1001)] + [event(1001, 50, 2)]
h = Harness(events)
print(json.dumps({'head': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
                  'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'methods': {n.name: hashlib.sha256(ast.get_source_segment(source, n).encode()).hexdigest()
                              for n in methods}}))
h.repair(UUID(events[0].envelope['event_id']))
after_repair = h.status()
h.cycle()
after_cycle = h.status()
refusals = {}
for label, e in [('held_successor', events[-1]), ('resolved_root', events[0])]:
    try:
        h.repair(UUID(e.envelope['event_id']))
    except ConsumerError as error:
        refusals[label] = error.code
print(json.dumps({'probe': 'observed_held_successor_beyond_first_repair_page',
                  'after_root_repair': after_repair, 'after_normal_cycle': after_cycle,
                  'subsequent_repair_refusals': refusals}, sort_keys=True))
assert after_cycle['scan_cursor'] == 1001 and after_cycle['applied_cursor'] == 1000
assert after_cycle['outcomes'] == {1001: 'held'} and after_cycle['sink_calls'] == [1]
assert after_cycle['target'][str(UUID(int=50))] == 1
assert refusals == {'held_successor': 'repair_event_not_poison', 'resolved_root': 'repair_event_missing'}
