"""Exact consumer AST flow with the previously reviewed in-memory ledger model.

Only constructor-owned state/events/hooks are supplied by this probe. No product
imports, PostgreSQL, containers, cloud, network or target engines execute.
"""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

root = Path(sys.argv[1])
source = root / 'next/src/cortex_core/modules/consumer.py'
head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
assert head == '86bc36c7d9769b87f0ccb6c4df309a8d0e477242'
scaffold_path = Path('/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/pr71-c07-repair-mike-review-20261011/page-boundary.py')
scaffold = scaffold_path.read_text().split('\nevents = [event(1, 50)]')[0]
context = {}
exec(compile(scaffold, '<reviewed-ledger-scaffold>', 'exec', dont_inherit=True), context)
Harness, event, ConsumerError = (context[n] for n in ('Harness', 'event', 'ConsumerError'))
events = [event(1, 50)] + [event(cursor, 10000 + cursor) for cursor in range(2, 1001)] + [event(1001, 50, 2)]
results = []

# Uninterrupted positive with the same exact AST and same durable ledger model.
positive = Harness(events)
positive.repair(UUID(events[0].envelope['event_id']))
positive.cycle()
assert positive.state['applied_cursor'] == positive.state['scan_cursor'] == 1001
assert positive.target[str(UUID(int=50))] == 2 and positive.calls == [1, 1001]
assert positive.outcomes == {}
results.append({'case': 'uninterrupted_original_1001', 'result': 'PASS', 'sink_calls': positive.calls,
                'applied_cursor': positive.state['applied_cursor'], 'scan_cursor': positive.state['scan_cursor']})

# Real public after_checkpoint hook fires only after the root's Core outcome
# has committed. Simulate worker interruption, then a new ordinary cycle.
interrupted = Harness(events)
raised = [False]


def after_root_commit():
    if not raised[0]:
        raised[0] = True
        raise TimeoutError('synthetic_crash_after_repaired_root_core_commit')


interrupted.after_checkpoint = after_root_commit
try:
    interrupted.repair(UUID(events[0].envelope['event_id']))
except ConsumerError as error:
    initial_error = error.code
else:
    raise AssertionError('after-checkpoint interruption must refuse')
assert initial_error == 'after_checkpoint'
persisted = interrupted.status()
assert persisted['applied_cursor'] == 1000 and persisted['scan_cursor'] == 1001
assert persisted['outcomes'] == {1001: 'held'} and persisted['sink_calls'] == [1]
assert interrupted.quarantined == set()
interrupted.after_checkpoint = None
interrupted.cycle()
after_cycle = interrupted.status()
refusals = {}
for name, e in [('resolved_root', events[0]), ('held_successor', events[-1])]:
    try:
        interrupted.repair(UUID(e.envelope['event_id']))
    except ConsumerError as error:
        refusals[name] = error.code
    else:
        raise AssertionError(name + ' should demonstrate current admission refusal')
assert refusals == {'resolved_root': 'repair_event_not_poison', 'held_successor': 'repair_event_not_poison'}
assert after_cycle == persisted and interrupted.target[str(UUID(int=50))] == 1
results.append({'case': 'after_root_checkpoint_interruption', 'result': 'STRANDED_HELD_SUCCESSOR',
                'initial_error': initial_error, 'persisted_after_interruption': persisted,
                'after_resumed_cycle': after_cycle, 'repair_refusals': refusals,
                'unresolved_quarantine': len(interrupted.quarantined)})

# A later target timeout must be distinguished from target_conflict poison.
transient = Harness(events)
original_apply = transient.apply


def fail_successor(e, generation):
    if e.cursor == 1001:
        raise TimeoutError('synthetic_transient_held_successor_unavailable')
    return original_apply(e, generation)


transient.sink = SimpleNamespace(apply=fail_successor)
try:
    transient.repair(UUID(events[0].envelope['event_id']))
except ConsumerError as error:
    transient_error = error.code
else:
    raise AssertionError('transient successor failure must refuse')
assert transient_error == 'target_unavailable' and transient.quarantined == set()
transient.sink = SimpleNamespace(apply=original_apply)
transient.cycle()
try:
    transient.repair(UUID(events[0].envelope['event_id']))
except ConsumerError as error:
    transient_retry = error.code
else:
    raise AssertionError('resolved root retry must demonstrate current refusal')
assert transient_retry == 'repair_event_not_poison'
assert transient.state['applied_cursor'] == 1000 and transient.outcomes == {1001: 'held'}
results.append({'case': 'transient_successor_failure_then_restoration', 'result': 'STRANDED_HELD_SUCCESSOR',
                'initial_error': transient_error, 'retry_error': transient_retry,
                'unresolved_quarantine': len(transient.quarantined), 'status': transient.status()})

result = {'head': head, 'consumer_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
          'scaffold_sha256': hashlib.sha256(scaffold_path.read_bytes()).hexdigest(),
          'source_methods': {n.name: hashlib.sha256(ast.get_source_segment(source.read_text(), n).encode()).hexdigest()
                             for n in context['methods']},
          'scope': 'exact source AST + prior declared SQL-ledger model; native NOT_RUN', 'cases': results}
Path(__file__).with_name('pagination-crash-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
