"""Private finite PostgreSQL client, composed through the existing P2 primitive.

The installed owner remains unbound. A connection, role name, operation UUID or
retained reference alone never admits an effect. The fixed P2 composition owns
the adapter, its connections/material IO and the current consumed dispatch.
Nothing here changes the migration manifest or U13's journal.
"""
from contextlib import contextmanager
import json
import re
import time
from uuid import UUID

from cortex_core.install import lifecycle, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw

__all__ = ['prepare_runtime_roles', 'bind_runtime_roles',
           'close_finite_roles', 'restore_runtime_roles']
_FIELDS = frozenset(('role', 'acl', 'database', 'hba', 'carrier'))
_SCHEMAS = ('core', 'auth', 'coordination', 'retrieval')
_SERVICES = ('api_request', 'probe', 'supervisor', 'observer', 'installer_enrollment')
_FLAGS = ('login', 'superuser', 'createdb', 'createrole', 'inherit', 'replication', 'bypassrls')
_SQL_FLAGS = ('LOGIN', 'SUPERUSER', 'CREATEDB', 'CREATEROLE', 'INHERIT', 'REPLICATION', 'BYPASSRLS')
_PRIVS = {'TABLE': ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN'),
          'SEQUENCE': ('USAGE','SELECT','UPDATE'), 'FUNCTION': ('EXECUTE',),
          'TYPE': ('USAGE',), 'SCHEMA': ('USAGE','CREATE'), 'DATABASE': ('CONNECT','TEMPORARY','CREATE')}


def _require(value, reason='resource_changed'):
    owner._require(value, reason)


def _sql_driver():
    # The existing protected SQL boundary is the first driver consumer. Pure
    # original-reference/default-refusal paths never require libpq or psycopg.
    try:
        import psycopg
    except ModuleNotFoundError as error:
        if error.name != 'psycopg':raise
        raise owner.OwnerRefusal('backend_unavailable') from None
    return psycopg


def _originals(history, context, *, expected_identity, reconciling=False):
    """Resolve all original bytes before any write; never snapshot changed state."""
    c = thaw(context['context'])
    _require(lifecycle._operation(c['identity']) == lifecycle._operation(expected_identity), 'scope_denied')
    event, observations = lifecycle._load_entry(history, context['journal_ref'])
    if not reconciling:
        attempt = lifecycle._decode(history.read(observations['attempt_ref']))
        if attempt.get('sql_mandate_ref') is not None:
            prefix = lifecycle._sql_prefix(history,thaw(context['journal_ref']),history.head()[0])
            _require(prefix['attempt']['dispatch_state']=='consumed_result_unknown','reservation_used')
        else:_require(history.head()[0] == thaw(context['journal_ref']), 'reservation_used')
    _require(event['identity'] == c['identity'] and event['effect'] == c['effect']
             and event['request_ref'] == c['request_ref'], 'scope_denied')
    attempt = lifecycle._decode(history.read(observations['attempt_ref']))
    reservation = lifecycle._decode(history.read(observations['reservation_ref']))
    _require(attempt['dispatch_state'] == 'consumed_result_unknown'
             and reservation['state'] == 'consumed', 'reservation_used')
    ref = lifecycle._retained_preimages(history, c['identity'])
    _require(thaw(context['preimages_ref']) == observations['preimages_ref'] == ref, 'journal_invalid')
    bundle = lifecycle._decode(history.read(ref))
    _require(set(bundle) == {'identity','originals'} and set(bundle['originals']) == _FIELDS,
             'journal_invalid')
    # _History.read independently measures each retained P1 RecordRef.
    return {key: history.read(bundle['originals'][key]) for key in sorted(_FIELDS)}


def _run(action, conn, names, operation, parameters):
    try:
        return _run_bound(action,conn,names,operation,parameters)
    except (owner.OwnerRefusal, ImportError):
        raise
    except Exception:
        raise owner.OwnerRefusal('backend_unavailable') from None


def _run_bound(action, conn, names, operation, parameters):
    bound = lifecycle._installed_lifecycle()  # fixed owner BEFORE caller connection access
    _require(type(bound) is lifecycle._Bindings and type(bound.primitives) is dict, 'backend_unavailable')
    primitive = bound.primitives.get('database_role')
    _require(type(primitive) is _Primitive and primitive.client.history is bound.history, 'backend_unavailable')
    adapter = primitive.client
    _require(type(names) is dict and names == adapter.roles and str(operation) == adapter.identity['operation_id']
             and action == primitive.action and parameters == primitive.parameters
             and conn is primitive.connection,
             'scope_denied')
    inputs = lifecycle._owner_inputs()
    context = owner._decode(inputs.context_raw,'owner_unavailable')
    ref = context['request_ref']
    _require(ref['path'] in inputs.originals, 'journal_invalid')
    request = inputs.originals[ref['path']]
    primitive._prime_sql()
    lifecycle.execute_effect(request)
    return adapter.receipt()


def prepare_runtime_roles(conn, *, role_names, operation_id):
    return _run('prepare',conn,role_names,operation_id,{})


def bind_runtime_roles(conn, *, installation_id, role_names, operation_id, embedding_job_kind, activate):
    return _run('bind',conn,role_names,operation_id,dict(installation_id=installation_id,
                embedding_job_kind=embedding_job_kind,activate=activate))


def close_finite_roles(conn, *, role_names, operation_id, owned_backend_pids):
    return _run('close',conn,role_names,operation_id,dict(owned_backend_pids=owned_backend_pids))


def restore_runtime_roles(recovery_conn, *, role_names, operation_id, preimage):
    return _run('restore',recovery_conn,role_names,operation_id,dict(preimage=thaw(preimage)))


class _Primitive:
    """P2's original-preimage view of the same finite client.

    `client.snapshot()` always measures live state. P2's snapshot port supplies
    the FIRST operation originals on every phase, even after an earlier phase
    changed the live catalogue. Its independent measure port always rereads live
    state. No later snapshot can replace an operation's original preimages.
    """
    def __init__(self, client, action, connection, parameters):
        _require(type(client) is _RoleAdapter and action in ('prepare','bind','migrate','close','restore'), 'backend_unavailable')
        _require(connection is (client.recovery if action=='restore' else client.connection), 'scope_denied')
        self.client, self.action, self.connection = client, action, connection
        self.parameters = dict(parameters)
        if action == 'migrate':
            client._migration_selection = dict(parameters)
        self.sql_mandate_raw = None

    def _prime_sql(self):
        """Private fixed composition DATA, ready before lifecycle takes its lock.

        Native observations use the EXISTING backend/private owner originals.
        None preserves the legacy source contract, never v2 qualification.
        """
        a=self.client;inputs=lifecycle._owner_inputs()
        c=owner._decode(inputs.context_raw,'owner_unavailable')
        consent=owner._original(inputs,c['consent_ref'])
        scope=owner._original(inputs,consent['scope_ref'])
        binding=owner._original(inputs,scope['backend_binding_ref'],'backend_unavailable')
        if binding.get('kind')!='pg_b2_hold1':
            _require(binding.get('protocol_version') is None,'backend_unavailable')
            self.sql_mandate_raw=None
            return
        _require(self.action in ('prepare','bind','migrate') and c['identity']==a.identity,'scope_denied')
        ref=c['request_ref']
        _require(ref['path'] in inputs.originals,'journal_invalid')
        admitted=owner.propose_effect(inputs.originals[ref['path']])
        lifecycle._continuity(c,thaw(admitted.context))
        a._native(c,scope['backend_binding_ref'])
        live=a.snapshot()
        for raw in inputs.originals.values():a.history.retain(raw)
        before=a.history.retain(canonical_bytes({'originals':{
            k:a.history.retain(live[k]) for k in sorted(_FIELDS)}}))
        self.sql_mandate_raw=canonical_bytes({'protocol_version':2,'identity':c['identity'],
            'effect':c['effect'],'request_ref':c['request_ref'],'phase':self.action,
            'phase_before_ref':before,'native_binding_ref':scope['backend_binding_ref']})

    def snapshot(self):
        live = self.client.snapshot()
        try:
            ref = lifecycle._retained_preimages(self.client.history,self.client.identity)
        except owner.OwnerRefusal as error:
            if error.detail != 'scope_denied':raise
            return live
        bundle = lifecycle._decode(self.client.history.read(ref))
        _require(set(bundle['originals']) == _FIELDS,'journal_invalid')
        return {k:self.client.history.read(bundle['originals'][k]) for k in sorted(_FIELDS)}

    def dispatch(self, context):
        a = self.client
        _require(a.context is None,'reservation_used')
        _originals(a.history,context,expected_identity=a.identity)
        if a._mandated(context):
            prefix=lifecycle._sql_prefix(a.history,thaw(context['journal_ref']),a.history.head()[0])
            mandate=lifecycle._decode(a.history.read(prefix['attempt']['sql_mandate_ref']))
            _require(self.action in ('prepare','bind','migrate') and mandate['phase']==self.action,'scope_denied')
        a.context = context
        try:
            a.admit(self.connection)
            if self.action == 'bind':
                p = self.parameters
                _require(type(p['activate']) is bool and isinstance(p['installation_id'],UUID)
                         and p['installation_id']==a.installation_id and p['embedding_job_kind']==a.embedding_kind
                         and type(a.embedding_kind) is str and 0<len(a.embedding_kind)<=256,'scope_denied')
                a.bind(self.connection,p['activate'])
            elif self.action == 'migrate':
                a.migrate(self.connection, **self.parameters)
            elif self.action == 'close':
                pids = self.parameters['owned_backend_pids']
                _require(type(pids) in (tuple,list) and len(set(pids))==len(pids)
                         and set(pids)==set(a.backends),'scope_denied')
                a.close(self.connection)
            elif self.action == 'restore':
                _require(self.parameters['preimage']==lifecycle._retained_preimages(a.history,a.identity),'journal_invalid')
                a.restore(self.connection)
            else:a.prepare(self.connection)
            a._current()
        except BaseException:
            a.created.clear()
            raise
        finally:
            a.context = None

    def measure(self, context):
        try:
            return self.client.measure(context,restoring=self.action=='restore')
        finally:
            self.client.created.clear()


class _RoleAdapter:
    """ONE existing snapshot/dispatch/measure primitive; no public constructor.

    The protected installed composition must supply the actual policy, original
    connection/role/database bindings, owned backend lifetimes and material IO.
    _Primitive holds its compiled private finite action, not request data or an
    externally selected phase channel. Source fixtures can bind this privately;
    they do not confer native/installed owner qualification.
    """
    def __init__(self, *, history, identity, connection, recovery_connection, role_names,
                 installation_id, embedding_job_kind, policy, materials, backends):
        _require(type(history) is lifecycle._History and type(role_names) is dict
                 and set(role_names) == set(policy['roles']) and len(set(role_names.values())) == len(role_names),
                 'backend_unavailable')
        _require(all(type(v) is str and 0 < len(v.encode()) <= 63 and '\0' not in v for v in role_names.values()),
                 'backend_unavailable')
        _require(isinstance(installation_id,UUID) and str(installation_id)==identity['installation_id'],
                 'backend_unavailable')
        self.history, self.identity = history, thaw(identity)
        self.connection, self.recovery = connection, recovery_connection
        self.roles, self.policy = dict(role_names), thaw(policy)
        self.installation_id, self.embedding_kind = installation_id, embedding_job_kind
        self.materials, self.backends = materials, dict(backends)
        self.context = None
        self.created = {}
        self._sql_transaction = None
        if connection is not None:self._sql_capability(connection)
        self._sql_capability(recovery_connection)
        self.database = self._database(recovery_connection)

    def _sql_capability(self, conn):
        driver = _sql_driver()  # availability BEFORE inspecting any connection
        _require(conn is not None and (conn is self.connection or conn is self.recovery), 'scope_denied')
        active = getattr(self, '_sql_transaction', None)
        # This is only the actual driver's context object held during our one
        # existing transaction manager, never a journal/token/second state store.
        for candidate in (self.connection, self.recovery):
            if candidate is None:continue
            # Finite close intentionally destroys its original socket. Recovery
            # measurement must remain possible; the selected handle is never skipped.
            if candidate is self.connection and candidate is not conn and getattr(candidate,'closed',False):continue
            status = candidate.info.transaction_status
            _require(candidate.autocommit is True and type(status) is driver.pq.TransactionStatus,
                     'atomicity_unavailable')
            if active is not None and candidate is active[0]:
                _require(active[1].savepoint_name in (None, '') and status == driver.pq.TransactionStatus.INTRANS,
                         'atomicity_unavailable')
            else:
                _require(status == driver.pq.TransactionStatus.IDLE, 'atomicity_unavailable')
        return driver

    def _execute(self, conn, *args, **kwargs):
        # Same existing connection/transaction, with the inherited finite clamp.
        # This is not a connection provider and supplies no owner authority.
        boundary = getattr(self, '_finite_boundary', None)
        # SET TRANSACTION must precede any snapshot-taking SELECT. Its fixed
        # control statement cannot wait on an application lock; the existing
        # timeout query follows it and still precedes every data/effect query.
        isolation = bool(args and type(args[0]) is str and args[0] in (
            'SET TRANSACTION ISOLATION LEVEL READ COMMITTED',
            'SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY'))
        if boundary is not None:
            self_stop = boundary()
            remaining = self_stop - time.monotonic_ns()
            _require(remaining > 0, 'reservation_expired')
            if isolation:
                result = conn.execute(*args, **kwargs)
                boundary()
            conn.execute("SELECT pg_catalog.set_config('statement_timeout',%s,false)",
                         (str(min(2000, max(1, remaining // 1_000_000))),))
            _require(time.monotonic_ns() < self_stop, 'reservation_expired')
        if boundary is None or not isolation:
            result = conn.execute(*args, **kwargs)
        if boundary is not None:
            boundary()
        return result

    def _mandated(self, context=None):
        context=getattr(self,'context',None) if context is None else context
        if context is None:return False
        _,obs=lifecycle._load_entry(self.history,context['journal_ref'])
        return lifecycle._decode(self.history.read(obs['attempt_ref'])).get('sql_mandate_ref') is not None

    @staticmethod
    def _native_proof(binding, private, identity):
        # Only the protected fixed observer may supply these native assertions.
        # This is conditional source validation, not an attestation issuer. The
        # installed observer is still unbound; SQL IDs alone never set a flag.
        proof=private.get('sql_native') if type(private) is dict else None
        _require(type(proof) is dict and binding.get('kind')=='pg_b2_hold1'
            and type(binding.get('protocol_version')) is int and binding['protocol_version']==2
            and all(type(binding.get(k)) is str and binding[k] for k in ('pg_lineage','p1_lineage')),
            'atomicity_unavailable')
        database=binding.get('database')
        _require(type(database) is list and len(database)==2 and type(database[0]) is int
            and database[0]>0 and type(database[1]) is str and database[1]
            and type(binding.get('system_identifier')) is str and binding['system_identifier']
            and type(binding.get('timeline_id')) is int and binding['timeline_id']>0,
            'resource_changed')
        _require(proof.get('continuous_pg') is True and proof.get('continuous_p1') is True
            and all(proof.get(k)==binding[k] for k in ('pg_lineage','p1_lineage')),
            'custody_changed')
        _require(all(proof.get(k) is True for k in ('durability_qualified','no_two_phase','no_post_seal_work'))
            and proof.get('writer_attempt_id')==identity['attempt_id'], 'atomicity_unavailable')
        return proof

    def _native(self, historical, binding_ref):
        inputs=lifecycle._owner_inputs();current=owner._decode(inputs.context_raw,'owner_unavailable')
        consent=owner._original(inputs,current['consent_ref'])
        scope=owner._original(inputs,consent['scope_ref'])
        _require(scope['backend_binding_ref']==binding_ref,'resource_changed')
        binding=owner._original(inputs,binding_ref,'backend_unavailable')
        private=owner._private(inputs,current)
        proof=self._native_proof(binding,private,historical['identity'])
        _require(proof.get('native_binding_ref')==binding_ref,'resource_changed')
        return binding,proof,current['private_binding']['binding_ref']

    def _sql_window(self, context, *, expected_context):
        deadline=context.get('deadline')
        inputs,_=lifecycle._admitted_deadline(deadline,expected_context=expected_context)
        now=owner._decode(inputs.current_clock_raw,'time_unavailable')
        budget=min((deadline.expires_at-now['now_utc']-now['uncertainty_seconds'])*1_000_000_000,
                   deadline.expires_monotonic_ns-now['monotonic_ns'])
        _require(budget>0,'reservation_expired')
        return time.monotonic_ns()+budget

    def _sql_barrier(self, conn, context, *, recovery=None):
        # Carry the already checked admission, keeping fresh recovery authority
        # separate from its historical SQL subject. Neither context is a permit.
        expected=thaw(recovery.context) if recovery is not None else thaw(context['context'])
        stop=self._sql_window(context,expected_context=expected)
        while True:
            lifecycle._admitted_deadline(context['deadline'],expected_context=expected)
            remaining=stop-time.monotonic_ns()
            _require(remaining>0,'reservation_expired')
            self._execute(conn, "SELECT pg_catalog.set_config('statement_timeout',%s,true)",
                         (str(min(2000,max(1,remaining//1_000_000))),))
            if self._execute(conn, 'SELECT pg_catalog.pg_try_advisory_xact_lock(%s)',(self.database[0],)).fetchone()==(True,):break
            time.sleep(min(.01,remaining/1_000_000_000))
        self._current(context,recovery=recovery)
        _require(time.monotonic_ns()<stop,'reservation_expired')

    def _sql_origin(self, conn):
        self._sql_capability(conn)
        xid=self._execute(conn, 'SELECT pg_catalog.pg_current_xact_id()::text').fetchone()[0]
        _require(type(xid) is str and re.fullmatch(r'[1-9][0-9]{0,19}',xid)
            and 3<=int(xid)<2**64,'attempt_ambiguous')
        cluster=self._execute(conn, 'SELECT c.system_identifier::text,p.timeline_id,pg_catalog.pg_is_in_recovery() '
            'FROM pg_catalog.pg_control_system() c CROSS JOIN pg_catalog.pg_control_checkpoint() p').fetchone()
        durability=self._execute(conn, "SELECT current_setting('synchronous_commit'),current_setting('fsync'),"
                               "current_setting('full_page_writes')").fetchone()
        _require(cluster and len(cluster)==3 and cluster[2] is False
            and type(cluster[0]) is str and type(cluster[1]) is int and cluster[1]>0,'resource_changed')
        _require(tuple(durability)==('on','on','on'),'atomicity_unavailable')
        return {'xid':xid,'cluster':list(cluster),'database':list(self._database(conn)[:2]),
                'backend_pid':conn.info.backend_pid,'durability':list(durability)}

    def _checkpoint(self, stage, observed):
        context=self.context;head=self.history.head()[0]
        prefix=lifecycle._sql_prefix(self.history,thaw(context['journal_ref']),head)
        attempt=dict(prefix['attempt'])
        fact={'stage':stage,'consumed_ref':thaw(context['journal_ref']),
              'sql_mandate_ref':attempt['sql_mandate_ref'],
              'sql_xact_ref':attempt['sql_xact_ref'] if stage=='postimage' else None,
              'observed':observed}
        attempt['sql_xact_ref' if stage=='xid' else 'sql_postimage_ref']=self.history.retain(canonical_bytes(fact))
        inputs=lifecycle._owner_inputs();clock=owner._decode(inputs.current_clock_raw,'time_unavailable')
        event=prefix['event']
        return lifecycle._publish_entry(self.history,context=prefix['context'],reservation=prefix['reservation'],
            attempt=attempt,preimages_ref=prefix['preimages_ref'],request_ref=event['request_ref'],
            release_ref=event['release_binding_ref'],expected_ref=head,phase=event['phase'],
            recorded_at=clock['now_utc'],deadline=context['deadline'],
            expected_context=thaw(context['context']))

    def _sql_postconditions(self, conn, raw, phase, *, before, activate=None):
        _require(phase in ('prepare','bind','migrate'),'atomicity_unavailable')
        roles=lifecycle._decode(raw['role'])['roles']
        _require(len(roles)==len(self.roles) and {r[1] for r in roles}==set(self.roles.values())
            and all(type(r[0]) is int and r[0]>0 for r in roles),'resource_changed')
        for r in roles:
            logical=next(k for k,v in self.roles.items() if v==r[1])
            if logical not in ('bootstrap','migrator'):
                _require(not any(r[n] for n in (3,4,5,6,7,8)),'resource_changed')
                if logical in ('request_group','verifier_owner'):_require(not r[2],'resource_changed')
        if conn is self.connection:
            known=self._known_oids()
            _require(all(known.get(r[1])==r[0] for r in roles),'resource_changed')
        original=lifecycle._decode(before['role'])
        prior={r[1]:r for r in original['roles']}
        if phase=='migrate':
            selected = self._migration_selection
            database = lifecycle._decode(raw['database'])
            original_db = lifecycle._decode(before['database'])
            _require(database['ledger'] == sorted(selected['ledger'])
                and all(database[k] == original_db[k] for k in ('database','bindings'))
                and all(raw[k] == before[k] for k in ('role','hba','carrier')), 'resource_changed')
        elif phase=='prepare':
            _require(all(raw[k]==before[k] for k in _FIELDS-{'role'})
                and lifecycle._decode(raw['role'])['memberships']==original['memberships'],'resource_changed')
            for row in roles:
                if row[1] in prior:_require(row==prior[row[1]],'resource_changed')
                else:_require(len(row)==12 and all(v is False for v in row[2:9])
                    and row[9:]==[-1,None,None],'resource_changed')
        else:
            _require(type(activate) is bool,'journal_invalid')
            self.validate_acl(conn)
            for row in roles:
                old=prior.get(row[1]);_require(old is not None,'resource_changed')
                wanted=list(old)
                if activate and row[1] in {self.roles[k] for k in _SERVICES}:wanted[2]=True
                _require(row==wanted,'resource_changed')
            db=lifecycle._decode(raw['database']);old_db=lifecycle._decode(before['database'])
            _require(db['database']==old_db['database'] and type(db['bindings']) is list
                and len(db['bindings'])==len(_SERVICES) and type(old_db['bindings']) is list,'resource_changed')
            _require(all(type(r) is list and len(r)==10 for r in db['bindings']+old_db['bindings']),'resource_changed')
            old={r[2]:r for r in old_db['bindings']};oids={r[1]:r[0] for r in roles}
            _require({r[2] for r in db['bindings']}==set(_SERVICES),'resource_changed')
            for row in db['bindings']:
                logical=row[2];previous=old.get(logical)
                was_active=False if previous is None else previous[4]
                revision=1 if previous is None else previous[5]
                expected=[oids[self.roles[logical]],self.roles[logical],logical,str(self.installation_id),
                    True if activate else was_active,revision+int(activate and not was_active),
                    self.identity['operation_id'],self.database[0],self.database[2],
                    self.embedding_kind if logical=='observer' else None]
                _require(type(row[4]) is bool and type(row[5]) is int and 1<=row[5]<=owner._INT_MAX
                    and row==expected,'resource_changed')
            acl=lifecycle._decode(raw['acl']);old_acl=lifecycle._decode(before['acl'])
            _require(acl['defaults']==old_acl['defaults'] and
                [r[:3]+r[4:] for r in acl['objects']]==[r[:3]+r[4:] for r in old_acl['objects']],
                'resource_changed')

    @contextmanager
    def _sql_transactional(self, conn, transaction, *, activate=None):
        context=self.context
        prefix=lifecycle._sql_prefix(self.history,thaw(context['journal_ref']),self.history.head()[0])
        mandate=lifecycle._decode(self.history.read(prefix['attempt']['sql_mandate_ref']))
        _require(mandate['phase'] in ('prepare','bind','migrate'),'atomicity_unavailable')
        self._execute(conn, 'SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        binding,_,proof_ref=self._native(thaw(context['context']),mandate['native_binding_ref'])
        self._sql_barrier(conn,context)
        self.admit(conn)
        # Configuration/assignment precedes managed role effects. The original
        # transaction must keep this exact durability through COMMIT.
        self._execute(conn, 'SET LOCAL synchronous_commit=on')
        origin=self._sql_origin(conn)
        _require(origin['database']==binding['database'] and origin['cluster'][:2]
            ==[binding['system_identifier'],binding['timeline_id']],'resource_changed')
        before=lifecycle._decode(self.history.read(mandate['phase_before_ref']))
        before={k:self.history.read(v) for k,v in before['originals'].items()}
        _require(self._snapshot(conn)==before,'resource_changed')
        self._checkpoint('xid',{**origin,'native_binding_ref':mandate['native_binding_ref'],
                                'native_proof_ref':proof_ref})
        self._current()
        yield
        _require(self._sql_transaction is not None and self._sql_transaction[0] is conn
            and self._sql_transaction[1] is transaction,'atomicity_unavailable')
        self._execute(conn, 'SET CONSTRAINTS ALL IMMEDIATE')
        self._current();self._native(thaw(context['context']),mandate['native_binding_ref'])
        _require(self._sql_origin(conn)==origin,'atomicity_unavailable')
        raw=self._snapshot(conn);self._sql_postconditions(conn,raw,mandate['phase'],before=before,activate=activate)
        _require(all(raw[k]==before[k] for k in ('hba','carrier')),'resource_changed')
        self._checkpoint('postimage',{'origin':origin,'activate':activate,'originals':{
            k:self.history.retain(raw[k]) for k in sorted(_FIELDS)}})
        self._current()
        self._native(thaw(context['context']),mandate['native_binding_ref'])
        _require(self._sql_transaction is not None and self._sql_transaction[0] is conn
            and self._sql_transaction[1] is transaction and self._sql_origin(conn)==origin
            and self._snapshot(conn)==raw,'atomicity_unavailable')

    @contextmanager
    def _measure_sql(self, context, *, recovery=None):
        historical=thaw(context['context']);head=self.history.head()[0]
        self._current(context,recovery=recovery)
        prefix=lifecycle._sql_prefix(self.history,thaw(context['journal_ref']),head)
        attempt=prefix['attempt'];mandate=lifecycle._decode(self.history.read(attempt['sql_mandate_ref']))
        try:binding,proof,_=self._native(historical,mandate['native_binding_ref'])
        except owner.OwnerRefusal:
            if recovery is None:raise
            yield ('UNKNOWN',None)
            return
        if recovery is not None and proof.get('writers_ended') is not True:
            # An absent checkpoint is usable only after permanent old-writer
            # fencing. Committed observations also cannot release a live writer.
            yield ('UNKNOWN',None)
            return
        conn=self.recovery
        _require(conn is not self.connection and self._sql_transaction is None,'atomicity_unavailable')
        self._sql_capability(conn)
        with conn.transaction() as transaction:
            _require(transaction.savepoint_name in (None, ''),'atomicity_unavailable')
            self._sql_transaction=(conn,transaction)
            try:
                self._execute(conn, 'SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY')
                self._sql_barrier(conn,context,recovery=recovery)
                # All status/current-state reads occur AFTER the lock statement.
                raw=self._snapshot(conn)
                answer=('UNKNOWN',None)
                if attempt['dispatch_state']!='consumed_result_unknown':
                    saved=self.history.read(prefix['event']['result_ref']);result=lifecycle._decode(saved)
                    refs=result['observed'].get('originals')
                    if (result['observed'].get('adapter')=='finite_pg_roles' and type(refs) is dict
                            and set(refs)==_FIELDS and raw=={k:self.history.read(v) for k,v in refs.items()}):
                        answer=('APPLIED' if result['state']=='applied_verified' else 'NOT_APPLIED',saved)
                else:
                    state=None;xid_ref=attempt.get('sql_xact_ref');post_ref=attempt.get('sql_postimage_ref')
                    before=lifecycle._decode(self.history.read(mandate['phase_before_ref']))
                    before={k:self.history.read(v) for k,v in before['originals'].items()}
                    if xid_ref is None:
                        if recovery is not None and raw==before:
                            state='not_applied_verified'
                    else:
                        xid=lifecycle._decode(self.history.read(xid_ref))['observed']
                        original_private=lifecycle._decode(self.history.read(xid['native_proof_ref']))
                        original_proof=self._native_proof(binding,original_private,historical['identity'])
                        _require(original_proof.get('native_binding_ref')==mandate['native_binding_ref']
                            and xid['native_binding_ref']==mandate['native_binding_ref']
                            and xid['database']==binding['database'] and xid['cluster']
                                ==[binding['system_identifier'],binding['timeline_id'],False]
                            and xid['durability']==['on','on','on'] and type(xid['xid']) is str
                            and re.fullmatch(r'[1-9][0-9]{0,19}',xid['xid'])
                            and 3<=int(xid['xid'])<2**64,'attempt_ambiguous')
                        cluster=self._execute(conn, 'SELECT c.system_identifier::text,p.timeline_id,pg_catalog.pg_is_in_recovery() '
                            'FROM pg_catalog.pg_control_system() c CROSS JOIN pg_catalog.pg_control_checkpoint() p').fetchone()
                        _require(list(cluster)==xid['cluster'],'resource_changed')
                        status=self._execute(conn, 'SELECT pg_catalog.pg_xact_status(%s::xid8)',(xid['xid'],)).fetchone()[0]
                        if status=='aborted' and raw==before:
                            state='not_applied_verified'
                        elif status=='committed' and post_ref is not None:
                            post=lifecycle._decode(self.history.read(post_ref))['observed']
                            expected={k:self.history.read(v) for k,v in post['originals'].items()}
                            if (set(expected)==_FIELDS and raw==expected and post['origin']=={
                                    k:xid[k] for k in ('xid','cluster','database','backend_pid','durability')}):
                                self._sql_postconditions(conn,raw,mandate['phase'],before=before,
                                                         activate=post.get('activate'))
                                state='applied_verified'
                    if state is not None:
                        result=canonical_bytes({'identity':historical['identity'],'effect':historical['effect'],
                            'request_ref':historical['request_ref'],'state':state,'observed':{
                                'adapter':'finite_pg_roles','originals':{k:self.history.retain(v) for k,v in raw.items()},
                                'sql_checkpoint_head':head}})
                        answer=('APPLIED' if state=='applied_verified' else 'NOT_APPLIED',result)
                self._current(context,recovery=recovery)
                _require(self.history.head()[0]==head,'generation_changed')
                if recovery is None:
                    _require(answer[0]=='APPLIED','attempt_ambiguous')
                    yield answer[1]
                else:yield answer
            finally:self._sql_transaction=None
        self._sql_capability(conn)

    def _database(self, conn):
        self._sql_capability(conn)
        return self._execute(conn, 'SELECT oid,datname,datdba,datallowconn,datconnlimit FROM pg_catalog.pg_database '
                            'WHERE datname=current_database()').fetchone()

    def _roles(self, conn):
        self._sql_capability(conn)
        rows = self._execute(conn, 'SELECT oid,rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,'
                            'rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil::text,rolconfig '
                            'FROM pg_catalog.pg_roles WHERE rolname=ANY(%s) ORDER BY rolname',
                            (list(self.roles.values()),)).fetchall()
        oids = [r[0] for r in rows]
        edges = self._execute(conn, 'SELECT roleid,member,grantor,admin_option,inherit_option,set_option '
                             'FROM pg_catalog.pg_auth_members WHERE roleid=ANY(%s) OR member=ANY(%s) '
                             'ORDER BY roleid,member,grantor', (oids,oids)).fetchall()
        return {'roles': [list(r) for r in rows], 'memberships': [list(r) for r in edges]}

    def _acl(self, conn):
        # ACL text is retained without normalizing NULL to effective defaults.
        self._sql_capability(conn)
        rows = self._execute(conn, "SELECT 'TABLE',c.oid,c.relowner,c.relacl::text[],"
                            "format('%%I.%%I',n.nspname,c.relname),c.relkind::text "
                            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=ANY(%s) AND c.relkind IN ('r','p','v','m','S','f') "
                            "UNION ALL SELECT 'FUNCTION',p.oid,p.proowner,p.proacl::text[],"
                            "p.oid::regprocedure::text,'' FROM pg_catalog.pg_proc p "
                            "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY(%s) "
                            "UNION ALL SELECT 'TYPE',t.oid,t.typowner,t.typacl::text[],"
                            "format('%%I.%%I',n.nspname,t.typname),'' FROM pg_catalog.pg_type t "
                            "JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace "
                            "WHERE n.nspname=ANY(%s) AND t.typtype IN ('d','e') "
                            "UNION ALL SELECT 'SCHEMA',oid,nspowner,nspacl::text[],quote_ident(nspname),'' "
                            "FROM pg_catalog.pg_namespace WHERE nspname=ANY(%s) "
                            "UNION ALL SELECT 'DATABASE',oid,datdba,datacl::text[],quote_ident(datname),'' "
                            "FROM pg_catalog.pg_database WHERE datname=current_database() ORDER BY 1,2",
                            (list(_SCHEMAS),list(_SCHEMAS),list(_SCHEMAS),[*_SCHEMAS,'public'])).fetchall()
        defaults = self._execute(conn, 'SELECT defaclrole,defaclnamespace,defaclobjtype::text,defaclacl::text[] '
                                'FROM pg_catalog.pg_default_acl WHERE defaclrole IN '
                                '(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)) '
                                'ORDER BY defaclrole,defaclnamespace,defaclobjtype',
                                (list(self.roles.values()),)).fetchall()
        return {'objects':[list(r) for r in rows], 'defaults':[list(r) for r in defaults]}

    def snapshot(self):
        _require(getattr(self, '_sql_transaction', None) is None, 'atomicity_unavailable')
        return self._snapshot(self.recovery)

    def _snapshot(self, conn):
        self._sql_capability(conn)
        material = self.materials.snapshot()
        _require(type(material) is dict and set(material) == {'hba','carrier'}
                 and all(type(v) is bytes for v in material.values()), 'backend_unavailable')
        db = list(self._database(conn))
        _require(db[:2] == list(self.database[:2]))
        present = self._execute(conn, "SELECT to_regclass('auth.runtime_login_bindings') IS NOT NULL").fetchone()[0]
        bindings = self._execute(conn, 'SELECT role_oid,role_name::text,logical_role,installation_id::text,active,revision,'
                                'operation_id::text,database_oid,database_owner_oid,embedding_job_kind '
                                'FROM auth.runtime_login_bindings ORDER BY role_oid').fetchall() if present else None
        database = {'database':db,'bindings':None if bindings is None else [list(r) for r in bindings]}
        if getattr(self, '_migration_selection', None) is not None:
            present = self._execute(conn, "SELECT to_regclass('core.schema_migrations') IS NOT NULL").fetchone()[0]
            ledger = self._execute(conn, 'SELECT migration_id,sha256 FROM core.schema_migrations').fetchall() if present else []
            database['ledger'] = sorted([list(row) for row in ledger])
        return {'role':canonical_bytes(self._roles(conn)), 'acl':canonical_bytes(self._acl(conn)),
                'database':canonical_bytes(database), **material}

    def _current(self, context=None, *, recovery=None):
        context=self.context if context is None else context
        _require(context is not None, 'scope_denied')
        raw = _originals(self.history, context, expected_identity=self.identity,
                         reconciling=recovery is not None)
        event, observation = lifecycle._load_entry(self.history, context['journal_ref'])
        inputs = lifecycle._owner_inputs()
        request = self.history.read(event['request_ref'])
        if recovery is not None:
            # Only measure supplies this freshly admitted common-owner proposal.
            # Dispatch/admit retain the historical continuity/deadline below.
            _require(type(recovery) is owner.OwnerProposal, 'scope_denied')
            original = lifecycle._decode(self.history.read(observation['context_ref']))
            c = thaw(context['context'])
            lifecycle._continuity(original, c)
            _require(c['identity'] == self.identity and c['effect']['resource_kind']=='database_role', 'scope_denied')
            ref = recovery.context['request_ref']
            _require(ref['path'] in inputs.originals, 'journal_invalid')
            fresh_request = inputs.originals[ref['path']]
            current = owner._evaluate(fresh_request, inputs, previous_deadline=recovery.deadline)
            owner._same_context(recovery, current)
            fresh = thaw(current.context)
            q = thaw(lifecycle.parse_install_message(fresh_request, expected_kind='request').document)
            old = thaw(lifecycle.parse_install_message(request, expected_kind='request').document)
            _require(q['operation']=='recover' and q['journal_ref']==thaw(context['journal_ref'])
                     and q['identity']['parent_attempt_id']==c['identity']['attempt_id'], 'scope_denied')
            _require(all(q['identity'][k]==c['identity'][k] for k in
                         ('product','installation_id','operation_id','transaction_id'))
                     and all(q['identity'][k]!=c['identity'][k] for k in ('request_id','attempt_id')),
                     'scope_denied')
            _require({k:v for k,v in q.items() if k not in ('identity','operation','journal_ref')}
                     == {k:v for k,v in old.items() if k not in ('identity','operation','journal_ref')}
                     and fresh['effect']==c['effect'] and fresh['target']==c['target'], 'scope_denied')
            _require(all(fresh[k]!=original[k] for k in ('owner_epoch','session_id')),
                     'reservation_invalidated')
            _require(fresh['host_lease']['holder']==original['host_lease']['holder']
                     and fresh['host_lease']['fence']>original['host_lease']['fence'], 'fence_changed')
            _require(all(fresh[k]>=c[k] for k in ('policy_generation','freshness_generation','state_revision'))
                     and fresh['state_revision']==self.history.current_revision(), 'generation_changed')
            _require(fresh['private_binding']['kind']==original['private_binding']['kind'], 'scope_denied')
            if original['private_binding']['kind']=='private':
                old_private=lifecycle._decode(self.history.read(original['private_binding']['binding_ref']))
                private=owner._private(inputs,fresh)
                _require(all(private[k]==old_private[k] for k in
                             ('phase','holder','session_user_oid','session_user_name','installation_id','operation_id'))
                         and private['fence']>old_private['fence'], 'fence_changed')
            for reference, previous in self.history.events():
                _, obs = lifecycle._load_entry(self.history,reference)
                held = lifecycle._decode(self.history.read(obs['context_ref']))
                _require(all(fresh['identity'][k]!=previous['identity'][k] for k in ('request_id','attempt_id')),
                         'reservation_used')
                _require(fresh['nonce']!=held['nonce'], 'nonce_replay')
            _, latest, obs = lifecycle._latest_attempt(self.history,c['identity'])
            attempt=lifecycle._decode(self.history.read(obs['attempt_ref']))
            reservation=lifecycle._decode(self.history.read(obs['reservation_ref']))
            _require(attempt['identity']==c['identity'] and attempt['effect']==c['effect']
                     and attempt['request_ref']==c['request_ref'] and latest['request_ref']==c['request_ref']
                     and obs['preimages_ref']==thaw(context['preimages_ref'])
                     and reservation['state']=='consumed'
                     and attempt['dispatch_state'] in ('consumed_result_unknown','applied_verified','not_applied_verified'),
                     'journal_invalid')
            return raw
        current = owner._evaluate(request, inputs)
        c = thaw(context['context'])
        lifecycle._continuity(c, thaw(current.context))
        _require(current.observed_state_revision == c['state_revision'], 'generation_changed')
        _require(c['effect']['resource_kind'] == 'database_role', 'scope_denied')
        reservation = lifecycle._decode(self.history.read(observation['reservation_ref']))
        clock = owner._decode(inputs.current_clock_raw, 'time_unavailable')
        lifecycle._before_deadline(clock, min(reservation['expires_at'],current.deadline.expires_at),
                                  min(reservation['expires_monotonic_ns'],current.deadline.expires_monotonic_ns))
        return raw

    def admit(self, conn):
        self._sql_capability(conn)
        originals=self._current()
        _require(conn is self.connection or conn is self.recovery, 'scope_denied')
        db = self._database(conn)
        _require(db[:2] == self.database[:2])
        if conn is self.connection:_require(db[2] == self.database[2])
        original_db=lifecycle._decode(originals['database'])['database']
        _require(list(db[:2]) == original_db[:2])
        if conn is self.connection:_require(db[2] == original_db[2])
        login = self._execute(conn, 'SELECT a.usesysid,r.oid,r.rolname,current_user '
                             'FROM pg_catalog.pg_stat_activity a JOIN pg_catalog.pg_roles r '
                             'ON r.rolname=session_user WHERE a.pid=pg_backend_pid()').fetchone()
        _require(login and login[0] == login[1] and login[2] == login[3], 'scope_denied')
        if conn is self.connection:
            # The fixed owner supplied this exact finite phase, not SET ROLE or
            # a runtime credential whose name happens to match an admin.
            private = (None if self.context is None else
                       owner._private(lifecycle._owner_inputs(), thaw(self.context['context'])))
            phase = 'bootstrap' if private is None else private['phase']
            _require(phase in ('bootstrap', 'migrator') and login[2] == self.roles[phase], 'scope_denied')
            if private is not None:
                _require(login[1] == private['session_user_oid'] and login[2] == private['session_user_name'],
                         'scope_denied')

    @contextmanager
    def _transaction(self, conn, *, activate=None):
        _require(getattr(self, '_sql_transaction', None) is None, 'atomicity_unavailable')
        self._sql_capability(conn)  # idle/autocommit: never borrow an outer transaction
        self.admit(conn)
        with conn.transaction() as transaction:
            _require(transaction.savepoint_name in (None, ''), 'atomicity_unavailable')
            self._sql_transaction = (conn, transaction)
            try:
                self._sql_capability(conn)
                if self._mandated():
                    with self._sql_transactional(conn,transaction,activate=activate):yield
                else:
                    self._execute(conn, 'SELECT pg_catalog.pg_advisory_xact_lock(%s)', (self.database[0],))
                    self.admit(conn)
                    yield
                    self._current()  # refuse drift before transactional SQL publication
            finally:
                self._sql_transaction = None
        # The driver's top-level context has COMMITted before any independent
        # snapshot/result publication. Never commit a caller's outer transaction.
        self._sql_capability(conn)

    def receipt(self):
        return {'operation_id':self.identity['operation_id'],
                'preimage':lifecycle._retained_preimages(self.history,self.identity)}

    def _known_oids(self, context=None, *, originals=None):
        context=self.context if context is None else context
        originals = (_originals(self.history,context,expected_identity=self.identity)
                     if originals is None else originals)
        original = lifecycle._decode(originals['role'])
        known = {r[1]:r[0] for r in original['roles']}
        # Actual created OIDs are independently measured in the same P1 result;
        # a name-only current catalogue lookup cannot adopt a foreign role.
        for _, event in self.history.events():
            if lifecycle._operation(event['identity']) == lifecycle._operation(self.identity) and event['result_ref']:
                result = lifecycle._decode(self.history.read(event['result_ref']))
                if result['observed'].get('adapter') == 'finite_pg_roles':
                    measured = lifecycle._decode(self.history.read(result['observed']['originals']['role']))
                    for row in measured['roles']:
                        if row[1] in self.roles.values():known.setdefault(row[1],row[0])
        for name,oid in self.created.items():known.setdefault(name,oid)
        return known

    def prepare(self, conn):
        sql = self._sql_capability(conn).sql
        original = lifecycle._decode(self._current()['role'])
        expected = self._known_oids()
        with self._transaction(conn):
            current = {r[1]:r for r in self._roles(conn)['roles']}
            for logical,name in self.roles.items():
                row = current.get(name)
                if row:
                    _require(expected.get(name) == row[0])
                    if logical not in ('bootstrap','migrator'):
                        _require(not any(row[n] for n in (3,4,5,6,7,8)))
                        if logical in ('request_group','verifier_owner'):_require(not row[2])
                else:
                    _require(name not in expected)
                    self._execute(conn, sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                                         'NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(name)))
                    self.created[name] = self._execute(conn, 'SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s',
                                                      (name,)).fetchone()[0]
        return self.receipt()

    def _objects(self):
        p = self.policy
        values = [('TABLE',row['name'],row['direct_privileges']) for row in p['tables']]
        values += [('FUNCTION',row['signature'],row['direct_privileges']) for row in p['functions']]
        for kind,key in [('TABLE','view'),('SEQUENCE','sequence'),('TYPE','type')]:
            values.append((kind,p[key]['name'],p[key]['direct_privileges']))
        return values

    def _plans(self):
        plans=[]
        for logical in _SERVICES:
            plans.append((logical,'DATABASE',self.database[1],['CONNECT']))
            for schema in (*_SCHEMAS,'public'):
                wanted=['USAGE'] if any(x.startswith('USAGE '+schema) for x in self.policy['schemas'].get(logical,[])) else []
                plans.append((logical,'SCHEMA',schema,wanted))
        ledger = self.policy['control_ledger']
        _require(ledger['service_privileges'] == ledger['verifier_privileges'] == [], 'backend_unavailable')
        plans.extend((logical,'TABLE',ledger['name'],[])
                     for logical in (*_SERVICES,'request_group','verifier_owner'))
        plans.extend((logical,kind,name,rights[logical]) for kind,name,rights in self._objects() for logical in _SERVICES)
        _require(all(set(wanted)<=set(_PRIVS[kind]) for _,kind,_,wanted in plans),'backend_unavailable')
        return plans

    def _privileges(self, conn, plans):
        self._sql_capability(conn)
        cells=[(logical,kind,name,privilege,privilege in wanted)
               for logical,kind,name,wanted in plans for privilege in _PRIVS[kind]]
        expressions=[]
        for option in (''," || ' WITH GRANT OPTION'"):
            expressions.append('CASE q.kind '+ ' '.join(
                "WHEN '"+kind+"' THEN pg_catalog.has_"+fn+"_privilege(q.role_name,q.object_name,q.privilege"+option+")"
                for kind,fn in [('TABLE','table'),('SEQUENCE','sequence'),('FUNCTION','function'),
                                ('TYPE','type'),('SCHEMA','schema'),('DATABASE','database')])+ ' END')
        query='SELECT q.n,'+','.join(expressions)+' FROM unnest(%s::text[],%s::text[],%s::text[],%s::text[]) WITH ORDINALITY AS q(role_name,kind,object_name,privilege,n) ORDER BY q.n'
        rows=self._execute(conn, query,([self.roles[c[0]] for c in cells],[c[1] for c in cells],
                                 [c[2] for c in cells],[c[3] for c in cells])).fetchall()
        _require(len(rows)==len(cells) and [r[0] for r in rows]==list(range(1,len(cells)+1)))
        return [(cell,row[1],row[2]) for cell,row in zip(cells,rows)]

    def _apply_grants(self, conn, plans):
        sql = self._sql_capability(conn).sql
        for (logical,kind,name,privilege,want),actual,grant_option in self._privileges(conn,plans):
            if actual != want or grant_option:
                verb='GRANT '+privilege+' ON '+kind+' {} TO {}' if want else 'REVOKE '+privilege+' ON '+kind+' {} FROM {}'
                target=sql.Identifier(name) if kind in ('DATABASE','SCHEMA') else sql.SQL(name)
                self._execute(conn, sql.SQL(verb).format(target,sql.Identifier(self.roles[logical])))
                if grant_option:
                    self._execute(conn, sql.SQL('REVOKE GRANT OPTION FOR '+privilege+' ON '+kind+' {} FROM {} CASCADE')
                                 .format(target,sql.Identifier(self.roles[logical])))

    def validate_acl(self, conn):
        sql = self._sql_capability(conn).sql
        for (_,_,_,_,want),actual,grant_option in self._privileges(conn,self._plans()):
            _require(actual==want and not grant_option)
        acl = self._acl(conn)
        expected_objects = {('FUNCTION',row['signature']) for row in self.policy['functions']}
        expected_objects |= {('TABLE',row['name']) for row in self.policy['tables']}
        expected_objects |= {('TABLE',self.policy[k]['name']) for k in ('view','sequence','control_ledger')}
        expected_objects |= {('TYPE',self.policy['type']['name'])}
        expected_oids=set()
        for kind,name in expected_objects:
            resolver={'FUNCTION':'to_regprocedure','TYPE':'to_regtype','TABLE':'to_regclass'}[kind]
            oid=self._execute(conn, sql.SQL('SELECT pg_catalog.'+resolver+'(%s)::oid'),(name,)).fetchone()[0]
            _require(oid is not None)
            expected_oids.add((kind,oid))
        actual_objects = {(kind,oid) for kind,oid,_,_,_,_ in acl['objects'] if kind not in ('SCHEMA','DATABASE')}
        _require(actual_objects == expected_oids)
        for kind,oid,object_owner,entries,_,relkind in acl['objects']:
            if kind == 'SCHEMA':continue
            acl_kind = {'TABLE':'s' if relkind=='S' else 'r','FUNCTION':'f','TYPE':'T','DATABASE':'d'}[kind]
            _require(not self._execute(conn, 'SELECT 1 FROM pg_catalog.aclexplode('
                                      'COALESCE(%s::aclitem[],pg_catalog.acldefault(%s::"char",%s::oid))) WHERE grantee=0',
                                      (entries,acl_kind,object_owner)).fetchone())
        for creator,_,_,entries in acl['defaults']:
            _require(not self._execute(conn, 'SELECT 1 FROM pg_catalog.aclexplode(%s::aclitem[]) WHERE grantee=0',
                                      (entries,)).fetchone())
        for logical in ('bootstrap','migrator'):
            creator=self._execute(conn, 'SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s',(self.roles[logical],)).fetchone()[0]
            for kind in ('f','T'):
                rows=[r[3] for r in acl['defaults'] if r[:3]==[creator,0,kind]]
                _require(len(rows)<=1)
                _require(not self._execute(conn, 'SELECT 1 FROM pg_catalog.aclexplode('
                    'COALESCE(%s::aclitem[],pg_catalog.acldefault(%s::"char",%s::oid))) WHERE grantee=0',
                    (rows[0] if rows else None,kind,creator)).fetchone())
        nonfinite=[v for k,v in self.roles.items() if k not in ('bootstrap','migrator')]
        _require(not self._execute(conn, 'SELECT 1 FROM pg_catalog.pg_database d WHERE d.datdba IN '
            '(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)) OR '
            '(d.oid<>%s AND EXISTS(SELECT 1 FROM pg_catalog.aclexplode(d.datacl) a '
            'WHERE a.grantee IN (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)))) LIMIT 1',
            (nonfinite,self.database[0],nonfinite)).fetchone())
        _require(self._roles(conn)['memberships'] == self._expected_membership(conn))

    def _expected_membership(self, conn):
        self._sql_capability(conn)
        row = self._execute(conn, 'SELECT g.oid,m.oid FROM pg_catalog.pg_roles g,pg_catalog.pg_roles m '
                           'WHERE g.rolname=%s AND m.rolname=%s',
                           (self.roles['request_group'],self.roles['api_request'])).fetchone()
        # The grantor is retained and checked separately by the current exact
        # graph; no ADMIN/INHERIT or role-edge expansion is permitted.
        edges = self._roles(conn)['memberships']
        _require(len(edges)==1 and edges[0][:2]==list(row) and edges[0][3:]==[False,False,True])
        return edges

    def bind(self, conn, activate):
        sql = self._sql_capability(conn).sql
        known = self._known_oids()
        with self._transaction(conn,activate=activate):
            roles = {r[1]:r[0] for r in self._roles(conn)['roles']}
            _require(all(name in roles and known.get(name)==roles[name] for name in self.roles.values()))
            for row in self._roles(conn)['roles']:
                logical=next(k for k,v in self.roles.items() if v==row[1])
                if logical not in ('bootstrap','migrator'):
                    _require(not any(row[n] for n in (3,4,5,6,7,8)))
                    if logical in ('request_group','verifier_owner'):_require(not row[2])
            # Lock the existing protected map in a stable order, before changes.
            old = self._execute(conn, 'SELECT role_oid,role_name::text,logical_role,installation_id,active,revision,'
                               'operation_id,database_oid,database_owner_oid,embedding_job_kind '
                               'FROM auth.runtime_login_bindings ORDER BY role_oid FOR UPDATE').fetchall()
            for row in old:
                _require(row[1] in self.roles.values() and str(row[3])==str(self.installation_id)
                         and str(row[6])==self.identity['operation_id'] and row[7:9]==self.database[::2][:2])
            for logical in _SERVICES:
                name = self.roles[logical]
                self._execute(conn, 'INSERT INTO auth.runtime_login_bindings '
                             '(role_oid,role_name,logical_role,installation_id,active,revision,operation_id,'
                             'database_oid,database_owner_oid,embedding_job_kind) '
                             'VALUES (%s,%s,%s,%s,false,1,%s,%s,%s,%s) ON CONFLICT (role_oid) DO NOTHING',
                             (roles[name],name,logical,self.installation_id,UUID(self.identity['operation_id']),
                              self.database[0],self.database[2],self.embedding_kind if logical=='observer' else None))
                bound = self._execute(conn, 'SELECT role_name::text,logical_role,installation_id,operation_id,'
                                     'database_oid,database_owner_oid,embedding_job_kind FROM auth.runtime_login_bindings '
                                     'WHERE role_oid=%s', (roles[name],)).fetchone()
                _require(bound==(name,logical,self.installation_id,UUID(self.identity['operation_id']),
                                  self.database[0],self.database[2],self.embedding_kind if logical=='observer' else None))
            edges = self._roles(conn)['memberships']
            group,member = roles[self.roles['request_group']],roles[self.roles['api_request']]
            _require(all(edge[:2]==[group,member] for edge in edges))
            if len(edges)!=1 or edges[0][3:]!=[False,False,True]:
                for edge in edges:
                    grantor = self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(edge[2],)).fetchone()[0]
                    self._execute(conn, sql.SQL('REVOKE {} FROM {} GRANTED BY {}')
                                 .format(sql.Identifier(self.roles['request_group']),sql.Identifier(self.roles['api_request']),sql.Identifier(grantor)))
                self._execute(conn, sql.SQL('GRANT {} TO {} WITH INHERIT FALSE,SET TRUE,ADMIN FALSE')
                             .format(sql.Identifier(self.roles['request_group']),sql.Identifier(self.roles['api_request'])))
            self._apply_grants(conn,self._plans())
            self.validate_acl(conn)
            if activate:
                for logical in _SERVICES:
                    self._execute(conn, sql.SQL('ALTER ROLE {} LOGIN').format(sql.Identifier(self.roles[logical])))
                self._execute(conn, 'UPDATE auth.runtime_login_bindings SET active=true,revision=revision+1 '
                             'WHERE operation_id=%s AND NOT active', (UUID(self.identity['operation_id']),))
        return self.receipt()

    def _demote_sql(self, conn):
        sql = self._sql_capability(conn).sql
        for logical in ('migrator', 'bootstrap'):
            self._current()
            self._execute(conn, sql.SQL('ALTER ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                                'NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(self.roles[logical])))
            self._current()

    def demote(self, conn):
        """Finite SQL postimage first; independent owner closes ALL sockets later.

        This method cannot itself prove final absence or release S05. It adds no
        privileged recovery connection and does not query through a killed one.
        """
        with self._transaction(conn):
            self._demote_sql(conn)
        self._current()
        return self.receipt()

    def migrate(self, conn, *, directory, through, manifest_sha256, ledger):
        """Reuse the sole migration engine and existing P1 SQL checkpoints."""
        from pathlib import Path
        import hashlib
        from cortex_core.migrations import apply_migrations

        selected = {'directory':directory, 'through':through, 'manifest_sha256':manifest_sha256, 'ledger':ledger}
        _require(getattr(self, '_migration_selection', None) == selected and type(ledger) is list
            and ledger and through == ledger[-1][0], 'scope_denied')
        path = Path(directory)/'manifest.json'

        def boundary(stage, connection):
            _require(connection is conn, 'scope_denied')
            self._current()
            _require(hashlib.sha256(path.read_bytes()).hexdigest() == manifest_sha256, 'resource_changed')
            self._current()

        @contextmanager
        def transaction_scope(connection, transaction):
            _require(self._sql_transaction is None and transaction.savepoint_name in (None, ''),
                     'atomicity_unavailable')
            self._sql_transaction = (connection, transaction)
            try:
                if self._mandated():
                    with self._sql_transactional(connection, transaction):
                        yield
                else:
                    self.admit(connection)
                    yield
                    self._current()
            finally:
                self._sql_transaction = None

        boundary('before_prepare', conn)
        return apply_migrations(conn, directory, through=through, _boundary=boundary,
                                _transaction_scope=transaction_scope, _execute=lambda *a, **k: self._execute(conn, *a, **k))

    def close(self, conn):
        driver = self._sql_capability(conn)
        sql = driver.sql
        finite=[self.roles['bootstrap'],self.roles['migrator']]
        query=('SELECT pid,usesysid,backend_start::text,datid FROM pg_catalog.pg_stat_activity '
               'WHERE usesysid IN (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=ANY(%s)) ORDER BY pid')
        current = self._execute(conn, query,(finite,)).fetchall()
        _require({r[0]:tuple(r[1:]) for r in current} == self.backends, 'scope_denied')
        self_pid = conn.info.backend_pid
        _require(self_pid in self.backends, 'scope_denied')
        with self._transaction(conn):
            # The database lock can wait. A PID set measured before that wait
            # cannot authorize terminating a different backend lifetime.
            _require({r[0]:tuple(r[1:]) for r in self._execute(conn, query,(finite,)).fetchall()} == self.backends,
                     'scope_denied')
            for pid,lifetime in self.backends.items():
                if pid != self_pid:
                    row=self._execute(conn, 'SELECT pg_catalog.pg_terminate_backend(pid,1000) '
                                     'FROM pg_catalog.pg_stat_activity WHERE pid=%s AND usesysid=%s '
                                     'AND backend_start::text=%s AND datid=%s',(pid,*lifetime)).fetchone()
                    _require(row and row[0])
            self._demote_sql(conn)
        # The original finite caller closes LAST. Independent recovery IO, not
        # a lost socket, decides whether all exact owned backend lifetimes drain.
        self._sql_capability(conn)
        try:
            self._execute(conn, 'SELECT pg_catalog.pg_terminate_backend(pg_backend_pid())')
        except driver.OperationalError:
            self._sql_capability(self.recovery)
            if self.recovery.execute(query,(finite,)).fetchall():
                raise
        self._sql_capability(self.recovery)
        _require(not self.recovery.execute(query,(finite,)).fetchall())
        return self.receipt()

    def _restore_acl(self, conn, original):
        sql = self._sql_capability(conn).sql
        current = self._acl(conn)
        self._restore_defaults(conn,original['defaults'],current['defaults'])
        observed = {(r[0],r[1]):r for r in current['objects']}
        _require(set(observed) == {(r[0],r[1]) for r in original['objects']})
        # Preflight object identities/NULL states before object ACL effects.
        for row in original['objects']:
            actual = observed[(row[0],row[1])]
            _require(actual[2] == row[2] and actual[4:] == row[4:])
            if row[3] is None:_require(actual[3] is None)
        for kind,oid,_,entries,name,relkind in original['objects']:
            actual = observed[(kind,oid)]
            if entries == actual[3]:continue
            target_kind = 'SEQUENCE' if relkind=='S' else kind
            grantees = self._execute(conn, 'SELECT DISTINCT grantee FROM pg_catalog.aclexplode(%s::aclitem[])',
                                    (actual[3],)).fetchall()
            for (grantee,) in grantees:
                role = sql.SQL('PUBLIC') if grantee==0 else sql.Identifier(self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(grantee,)).fetchone()[0])
                self._execute(conn, sql.SQL('REVOKE ALL ON '+target_kind+' {} FROM {} CASCADE').format(sql.SQL(name),role))
            rows = self._execute(conn, 'SELECT grantor,grantee,privilege_type,is_grantable '
                                'FROM unnest(%s::aclitem[]) WITH ORDINALITY a(item,n) '
                                'CROSS JOIN LATERAL pg_catalog.aclexplode(ARRAY[a.item]) ORDER BY n', (entries,)).fetchall()
            for grantor,grantee,privilege,option in rows:
                _require(privilege in _PRIVS[target_kind])
                recipient = sql.SQL('PUBLIC') if grantee==0 else sql.Identifier(self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(grantee,)).fetchone()[0])
                giver = sql.Identifier(self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(grantor,)).fetchone()[0])
                # Object GRANTED BY must be the current user, including for a
                # superuser recovery connection. Preserve the original grantor
                # through PostgreSQL's checked SET ROLE, never a catalogue edit.
                self._execute(conn, sql.SQL('SET LOCAL ROLE {}').format(giver))
                try:
                    self._execute(conn, sql.SQL('GRANT '+privilege+' ON '+target_kind+' {} TO {}'+(' WITH GRANT OPTION' if option else '')+' GRANTED BY {}')
                                 .format(sql.SQL(name),recipient,giver))
                finally:
                    self._execute(conn, 'RESET ROLE')

    def _restore_defaults(self, conn, original, current):
        sql = self._sql_capability(conn).sql
        before={tuple(row[:3]):row[3] for row in original}
        now={tuple(row[:3]):row[3] for row in current}
        kinds={'f':('FUNCTIONS','FUNCTION'),'T':('TYPES','TYPE'),'r':('TABLES','TABLE'),
               'S':('SEQUENCES','SEQUENCE'),'n':('SCHEMAS','SCHEMA')}
        for key in sorted(set(before)|set(now)):
            if before.get(key)==now.get(key):continue
            creator,namespace,kind=key
            _require(kind in kinds)
            creator_name=self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(creator,)).fetchone()[0]
            _require(creator_name in self.roles.values())
            prefix=sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {}').format(sql.Identifier(creator_name))
            if namespace:
                name=self._execute(conn, 'SELECT nspname FROM pg_catalog.pg_namespace WHERE oid=%s',(namespace,)).fetchone()
                _require(name and name[0] in (*_SCHEMAS,'public'))
                prefix += sql.SQL(' IN SCHEMA {}').format(sql.Identifier(name[0]))
            for (grantee,) in self._execute(conn, 'SELECT DISTINCT grantee FROM pg_catalog.aclexplode(%s::aclitem[])',
                                           (now.get(key),)).fetchall():
                recipient=sql.SQL('PUBLIC') if grantee==0 else sql.Identifier(self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(grantee,)).fetchone()[0])
                self._execute(conn, prefix+sql.SQL(' REVOKE ALL ON '+kinds[kind][0]+' FROM {} CASCADE').format(recipient))
            target=before.get(key)
            if target is None:
                target=self._execute(conn, 'SELECT pg_catalog.acldefault(%s::"char",%s::oid)::text[]',
                                    (kind,creator)).fetchone()[0] if namespace==0 else []
            rows=self._execute(conn, 'SELECT grantor,grantee,privilege_type,is_grantable '
                'FROM unnest(%s::aclitem[]) WITH ORDINALITY a(item,n) '
                'CROSS JOIN LATERAL pg_catalog.aclexplode(ARRAY[a.item]) ORDER BY n',(target,)).fetchall()
            for grantor,grantee,privilege,option in rows:
                _require(grantor==creator and privilege in _PRIVS[kinds[kind][1]])
                recipient=sql.SQL('PUBLIC') if grantee==0 else sql.Identifier(self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(grantee,)).fetchone()[0])
                self._execute(conn, prefix+sql.SQL(' GRANT '+privilege+' ON '+kinds[kind][0]+' TO {}'+(' WITH GRANT OPTION' if option else '')).format(recipient))

    def restore(self, conn):
        sql = self._sql_capability(conn).sql
        originals = self._current()  # read/measure ALL five first
        roles = lifecycle._decode(originals['role'])
        database = lifecycle._decode(originals['database'])
        acl = lifecycle._decode(originals['acl'])
        known = self._known_oids()
        with self._transaction(conn):
            current = {r[1]:r for r in self._roles(conn)['roles']}
            for row in roles['roles']:_require(row[1] in current and current[row[1]][0]==row[0])
            for name,row in current.items():_require(known.get(name)==row[0])
            db = database['database'];_require(list(self._database(conn))[:2] == db[:2])
            db_owner = self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(db[2],)).fetchone()[0]
            _require(known.get(db_owner)==db[2])
            self._execute(conn, sql.SQL('ALTER DATABASE {} OWNER TO {}')
                         .format(sql.Identifier(db[1]),sql.Identifier(db_owner)))
            self._restore_acl(conn,acl)
            for row in roles['roles']:
                # PostgreSQL's ALTER ROLE does not encode NULL as infinity;
                # preserve the original representation rather than relabel it.
                if row[10] is None:_require(current[row[1]][10] is None)
                flags = ' '.join(word if value else 'NO'+word for word,value in zip(_SQL_FLAGS,row[2:9]))
                self._execute(conn, sql.SQL('ALTER ROLE {} '+flags+' CONNECTION LIMIT {}')
                             .format(sql.Identifier(row[1]),sql.Literal(row[9])))
                if row[10] != current[row[1]][10]:
                    self._execute(conn, sql.SQL('ALTER ROLE {} VALID UNTIL {}')
                                 .format(sql.Identifier(row[1]),sql.Literal(row[10])))
                if current[row[1]][11] != row[11]:
                    self._execute(conn, sql.SQL('ALTER ROLE {} RESET ALL').format(sql.Identifier(row[1])))
                    for setting in row[11] or []:
                        key,value=setting.split('=',1)
                        self._execute(conn, sql.SQL('ALTER ROLE {} SET {} TO {}')
                                     .format(sql.Identifier(row[1]),sql.Identifier(key),sql.Literal(value)))
            edges = self._roles(conn)['memberships']
            for granted,member,grantor,admin,inherit,set_role in edges:
                if [granted,member,grantor,admin,inherit,set_role] not in roles['memberships']:
                    names = [self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(oid,)).fetchone()[0]
                             for oid in (granted,member)]
                    _require(all(name in self.roles.values() for name in names))
                    grantor_name=self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(grantor,)).fetchone()[0]
                    self._execute(conn, sql.SQL('REVOKE {} FROM {} GRANTED BY {}')
                                 .format(*map(sql.Identifier,names),sql.Identifier(grantor_name)))
            for granted,member,grantor,admin,inherit,set_role in roles['memberships']:
                if [granted,member,grantor,admin,inherit,set_role] not in edges:
                    names = [self._execute(conn, 'SELECT pg_catalog.pg_get_userbyid(%s)',(oid,)).fetchone()[0]
                             for oid in (granted,member,grantor)]
                    self._execute(conn, sql.SQL('GRANT {} TO {} WITH ADMIN {},INHERIT {},SET {} GRANTED BY {}')
                                 .format(sql.Identifier(names[0]),sql.Identifier(names[1]),sql.Literal(admin),
                                         sql.Literal(inherit),sql.Literal(set_role),sql.Identifier(names[2])))
            self._execute(conn, sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS {} CONNECTION LIMIT {}')
                         .format(sql.Identifier(db[1]),sql.Literal(db[3]),sql.Literal(db[4])))
            if database['bindings'] is not None:
                self._execute(conn, 'DELETE FROM auth.runtime_login_bindings WHERE operation_id=%s', (UUID(self.identity['operation_id']),))
                for row in database['bindings']:
                    self._execute(conn, 'INSERT INTO auth.runtime_login_bindings VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',row)
            original_names = {r[1] for r in roles['roles']}
            for name in sorted(set(current)-original_names):
                self._execute(conn, sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
            self.materials.restore({'hba':originals['hba'],'carrier':originals['carrier']})
            _require(self._snapshot(conn) == originals)  # same SQL transaction; P2 measure rereads after COMMIT
        return self.receipt()

    def measure(self, context, *, restoring=False):
        # P2's dispatcher independently rereads after the compiled finite work.
        c = thaw(context['context'])
        inputs=lifecycle._owner_inputs()
        fixed=owner._decode(inputs.context_raw,'owner_unavailable')
        ref=fixed['request_ref']
        _require(ref['path'] in inputs.originals,'journal_invalid')
        request=inputs.originals[ref['path']]
        try:
            q=thaw(lifecycle.parse_install_message(request,expected_kind='request').document)
        except lifecycle.InstallRefusal:
            raise owner.OwnerRefusal('invalid_request') from None
        # The existing measure port follows only the fixed current owner's
        # admitted recovery operation. No caller field/provider selects a mode.
        recovery=owner.propose_effect(request) if q['operation']=='recover' else None
        if self._mandated(context):return self._measure_sql(context,recovery=recovery)
        if recovery is not None:
            head=self.history.head()[0]
            originals=self._current(context,recovery=recovery)
            self._sql_capability(self.recovery)
        try:
            raw=self.snapshot()
        except owner.OwnerRefusal as error:
            if recovery is None or error.detail not in owner._DRIFT:raise
            raw=None
        if recovery is not None:
            answer=('UNKNOWN',None)
            _, event, obs=lifecycle._latest_attempt(self.history,c['identity'])
            attempt=lifecycle._decode(self.history.read(obs['attempt_ref']))
            state=attempt['dispatch_state']
            truth_ref=(attempt['reconciliation_ref'] if state=='consumed_result_unknown'
                       else attempt['result_ref'])
            _require(event['result_ref']==attempt['result_ref'],'journal_invalid')
            if truth_ref is not None and raw is not None:
                truth_raw=self.history.read(truth_ref)
                truth=lifecycle._decode(truth_raw)
                observed=truth.get('observed')
                refs=observed.get('originals') if type(observed) is dict else None
                if (set(truth)=={'identity','effect','request_ref','state','observed'}
                        and truth['identity']==c['identity'] and truth['effect']==c['effect']
                        and truth['request_ref']==c['request_ref']
                        and truth['state'] in ('applied_verified','not_applied_verified')
                        and (state=='consumed_result_unknown' or state==truth['state'])
                        and type(observed) is dict and observed.get('adapter')=='finite_pg_roles'
                        and type(refs) is dict and set(refs)==_FIELDS
                        and all(type(v) is dict and set(v)=={'path','bytes','sha256'} for v in refs.values())):
                    accepted={k:self.history.read(refs[k]) for k in sorted(_FIELDS)}
                    known=self._known_oids(context,originals=originals)
                    roles=lifecycle._decode(raw['role'])['roles']
                    original_roles=lifecycle._decode(originals['role'])['roles']
                    database=lifecycle._decode(raw['database'])['database']
                    identities=(all(type(row[0]) is int and row[0]>0 and type(row[1]) is str
                                    and known.get(row[1],row[0])==row[0] for row in roles)
                                and all(any(row[:2]==old[:2] for row in roles) for old in original_roles)
                                and type(database[0]) is int and database[0]>0
                                and type(database[2]) is int and database[2]>0)
                    if identities and raw==accepted:
                        if truth['state']=='applied_verified':answer=('APPLIED',truth_raw)
                        elif raw==originals:answer=('NOT_APPLIED',truth_raw)
            self._current(context,recovery=recovery)  # same fresh deadline, never the old consumed one
            _require(self.history.head()[0]==head,'generation_changed')
            return answer
        originals=_originals(self.history,context,expected_identity=self.identity)
        known=self._known_oids(context)
        _require(all(known.get(row[1])==row[0] for row in lifecycle._decode(raw['role'])['roles']))
        if restoring:_require(raw==originals)
        refs={k:self.history.retain(v) for k,v in raw.items()}
        self._current(context)  # final owner/deadline check after independent measurement IO
        return canonical_bytes({'identity':c['identity'],'effect':c['effect'],
            'request_ref':c['request_ref'],'state':'applied_verified',
            'observed':{'adapter':'finite_pg_roles','originals':refs}})
