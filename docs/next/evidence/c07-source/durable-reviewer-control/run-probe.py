"""Declared exact-AST case driver; unchanged9465118d reviewer SQL-ledger model.
Constructor-owned persistent state is transferred to fresh Harness instances.
No product import, database, engine, credential or network operation.
"""
import hashlib,json,subprocess,sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID
root=Path(sys.argv[1]).resolve()
model=Path(__file__).with_name('page-boundary-model.py')
assert hashlib.sha256(model.read_bytes()).hexdigest()=='9465118df9004ac0c8ee5f4bc5f931df34daf29d0071b8a9abd9f2a9706116d2'
context={}
exec(compile(model.read_text().split('\nevents = [event(1, 50)]')[0],str(model),'exec',dont_inherit=True),context)
Harness,event,ConsumerError=(context[x] for x in ('Harness','event','ConsumerError'))
events=[event(1,50)]+[event(c,10000+c) for c in range(2,1001)]+[event(1001,50,2)]
def fresh(old):
    new=Harness(events)
    new.state=dict(old.state);new.outcomes=dict(old.outcomes);new.quarantined=set(old.quarantined)
    new.target=dict(old.target);new.calls=list(old.calls)
    return new
positive=Harness(events);positive.repair(UUID(events[0].envelope['event_id']));positive.cycle()
rows=[dict(case='uninterrupted_original_1001',applied_cursor=positive.state['applied_cursor'],status=positive.status())]
interrupted=Harness(events)
def stop():raise TimeoutError('after_root_commit')
interrupted.after_checkpoint=stop
try:interrupted.repair(UUID(events[0].envelope['event_id']))
except ConsumerError as error:assert error.code=='after_checkpoint'
else:raise AssertionError('required interruption did not occur')
saved=interrupted.status();resumed=fresh(interrupted);resumed.cycle()
rows.append(dict(case='after_root_checkpoint_interruption',persisted_after_interruption=saved,after_resumed_cycle=resumed.status()))
transient=Harness(events);apply=transient.apply
def timeout(e,g):
    if e.cursor==1001:raise TimeoutError('temporary_held_failure')
    return apply(e,g)
transient.sink=SimpleNamespace(apply=timeout)
try:transient.repair(UUID(events[0].envelope['event_id']))
except ConsumerError as error:assert error.code=='target_unavailable'
else:raise AssertionError('required temporary failure did not occur')
retry=fresh(transient);retry.cycle()
rows.append(dict(case='transient_successor_failure_then_restoration',status=retry.status()))
source=root/'next/src/cortex_core/modules/consumer.py'
data=dict(head=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip(),consumer_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),model_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),scope='exact AST source flow, native NOT_RUN',cases=rows)
destination=Path(__file__).parent/'consumer/pagination-crash-result.json';destination.parent.mkdir(exist_ok=True)
destination.write_text(json.dumps(data,indent=2)+'\n');print(json.dumps(dict(head=data['head'],source_sha256=data['consumer_sha256'],cases=len(rows))))
