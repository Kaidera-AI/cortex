"""Exact consumer methods, declared scoped-feed/SQL-ledger model; no product imports.

The feed returns project-scoped rows and an installation-global head, matching
002-outbox.sql:295-301. The ledger folds/compacts local rows only, matching
003-module-consumers.sql:249-268. This is source-flow proof, not real PG proof.
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

ROOT = Path(sys.argv[1]).resolve()
OUTPUT = Path(sys.argv[2]).resolve()
HEADS = ('86bc36c7d9769b87f0ccb6c4df309a8d0e477242',
         '9f5cbd3b3ede6e074f9619da44c95d88747e61a7')
SOURCE = 'next/src/cortex_core/modules/consumer.py'
assert subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip() == HEADS[1]
assert (ROOT / SOURCE).read_bytes() == subprocess.check_output(['git', '-C', str(ROOT), 'show', HEADS[1]+':'+SOURCE])


class ConsumerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class OutboxError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def event(cursor, aggregate=50, revision=1):
    payload = ('event-' + str(cursor)).encode()
    return SimpleNamespace(cursor=cursor, payload=payload, envelope=dict(
        installation_id=str(UUID(int=1)), project_id=str(UUID(int=3)),
        event_id=str(UUID(int=2000+cursor)), aggregate_id=str(UUID(int=aggregate)),
        aggregate_kind='memory', aggregate_revision=revision, schema_version=1,
        tombstone=False, operation='upsert', payload_sha256=hashlib.sha256(payload).hexdigest()))


def load(head):
    raw = subprocess.check_output(['git', '-C', str(ROOT), 'show', head+':'+SOURCE])
    text = raw.decode()
    cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == 'ModuleConsumer')
    wanted = {'_state', '_validate', '_apply_event', 'cycle'}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in methods} == wanted
    namespace = dict(UUID=UUID, hashlib=hashlib, re=re, ConsumerError=ConsumerError, OutboxError=OutboxError)
    digests = {}
    for method in methods:
        segment = ast.get_source_segment(text, method)
        digests[method.name] = hashlib.sha256(segment.encode()).hexdigest()
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<exact-'+head+'-'+method.name+'>', 'exec'), namespace)
    return namespace, dict(head=head, consumer_sha256=hashlib.sha256(raw).hexdigest(), methods=digests)


def harness(namespace):
    class Harness:
        _state = namespace['_state']
        _validate = namespace['_validate']
        _apply_event = namespace['_apply_event']
        cycle = namespace['cycle']
        installation_id = UUID(int=1)
        project_id = UUID(int=3)
        module_id = 'graph'
        manifest = SimpleNamespace(schema_version=1, aggregate_kinds=('memory',))
        after_checkpoint = None
        max_pending_outcomes = 10000

        def __init__(self, events, head):
            self.events = list(events)
            self.head = head
            self.state = dict(generation=1, state='active', floor=0, applied_cursor=0, scan_cursor=0)
            self.outcomes = {}
            self.poison = set()
            self.target = {}
            self.calls = []
            self.pages = []
            self.transient = False
            self.commit_then_fail = False
            self.feed = SimpleNamespace(page=self.page)
            self.sink = SimpleNamespace(apply=self.apply)

        def hold_lock(self):
            return nullcontext()

        def page(self, after=0, limit=100):
            self.pages.append(dict(after=after, limit=limit, head=self.head))
            return SimpleNamespace(events=tuple(e for e in self.events if e.cursor > after)[:limit], head=self.head, floor=0)

        def status(self):
            return {**self.state, 'pending_outcomes': sum(c > self.state['applied_cursor'] for c in self.outcomes)}

        def _call(self, signature, args):
            if signature.startswith('c07_poisoned'):
                return str(args[-1]) in self.poison
            if signature.startswith('c07_scan'):
                cursor = args[-1]
                assert self.state['scan_cursor'] <= cursor <= self.head
                assert all(e.cursor in self.outcomes for e in self.events if self.state['scan_cursor'] < e.cursor <= cursor)
                self.state['scan_cursor'] = cursor
                return self.status()
            raise AssertionError(signature)

        def _record(self, state, e, outcome, error=None, receipt=None):
            aggregate = e.envelope['aggregate_id']
            if outcome == 'poison':
                self.poison.add(aggregate)
            elif outcome in ('applied', 'stale'):
                self.poison.discard(aggregate)
            self.outcomes[e.cursor] = outcome
            self.state['scan_cursor'] = max(self.state['scan_cursor'], e.cursor)
            applied = self.state['applied_cursor']
            for candidate in self.events:
                if candidate.cursor <= applied:
                    continue
                if candidate.cursor > self.state['scan_cursor']:
                    break
                if self.outcomes.get(candidate.cursor) not in ('applied', 'stale'):
                    break
                applied = candidate.cursor
            self.state['applied_cursor'] = applied
            self.outcomes = {c:o for c,o in self.outcomes.items() if not (c <= applied and o in ('applied','stale'))}

        def apply(self, e, generation):
            if self.transient:
                raise TimeoutError('temporary_target_failure')
            env = e.envelope
            aggregate = env['aggregate_id']
            self.calls.append(e.cursor)
            prior = self.target.get(aggregate)
            stale = prior is not None and prior['revision'] > env['aggregate_revision']
            if prior is None or prior['revision'] < env['aggregate_revision']:
                self.target[aggregate] = dict(revision=env['aggregate_revision'], apply_count=1 if prior is None else prior['apply_count']+1)
            if self.commit_then_fail:
                self.commit_then_fail = False
                raise TimeoutError('after_target_commit_before_ack')
            return dict(event_id=env['event_id'], aggregate_id=aggregate, revision=env['aggregate_revision'],
                        tombstone=env['tombstone'], payload_sha256=env['payload_sha256'], generation=generation, stale=stale)

        def snapshot(self):
            return dict(state=self.status(), outcomes=dict(self.outcomes), target=dict(self.target), calls=list(self.calls), pages=list(self.pages))
    return Harness


def run_cycle(port):
    try:
        port.cycle()
    except ConsumerError as error:
        return error.code
    return None


def scoped_cases(Harness):
    rows=[]
    for name, initial, head, later in (
        ('empty_project_foreign_head', [], 1, event(2)),
        ('completed_local_foreign_tail', [event(1)], 2, event(3, revision=2)),
    ):
        p=Harness(initial, head)
        assert run_cycle(p) is None
        first=p.snapshot()
        repeated=run_cycle(p)
        p.events.append(later)
        p.head=later.cursor
        future=run_cycle(p)
        rows.append(dict(case=name, after_first=first, repeated_cycle_error=repeated,
                         new_local_cycle_error=future, new_local_cursor=later.cursor, final=p.snapshot()))
    return rows


def durable_positives(Harness):
    rows=[]
    for name, fail in (('root_checkpoint_already_persisted',None),('transient_held_retry','transient'),('target_commit_before_ack_retry','commit_then_fail')):
        p=Harness([event(1),event(2,revision=2)],2)
        p.state.update(applied_cursor=1,scan_cursor=2)
        p.outcomes={2:'held'}
        p.target={str(UUID(int=50)):dict(revision=1,apply_count=1)}
        if fail:
            setattr(p,fail,True)
            assert run_cycle(p)=='target_unavailable'
            assert p.outcomes=={2:'held'}
            p.transient=False
        assert run_cycle(p) is None
        assert p.state['applied_cursor']==2 and p.outcomes=={}
        assert p.target[str(UUID(int=50))]==dict(revision=2,apply_count=2)
        rows.append(dict(case=name, final=p.snapshot()))
    return rows


results=[]
for head in HEADS:
    namespace,binding=load(head)
    Harness=harness(namespace)
    rows=scoped_cases(Harness)
    positives=durable_positives(Harness) if head==HEADS[1] else []
    results.append(dict(binding=binding, cases=rows, durable_positives=positives))
OUTPUT.write_text(json.dumps(dict(scope='exact source flow; PostgreSQL/native NOT_RUN', heads=results),indent=2)+'\n')
print(json.dumps({'output':str(OUTPUT),'prior_errors':[r['repeated_cycle_error'] for r in results[0]['cases']],
                  'current_errors':[r['repeated_cycle_error'] for r in results[1]['cases']],
                  'current_durable_positives':len(results[1]['durable_positives'])}))
