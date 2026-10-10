"""Private PG job state machine; released role/HTTP adapters and C06 capture are held."""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID, uuid4

import psycopg

from .auth import AuthError, authorized, _resolve
from .identity import lookup_bound
from .records import RecordError, _payload, _request, _save_request, _scope, _validate, MAX_PAYLOAD_BYTES

_LEASE_DEADLINE = ContextVar('c05_verified_lease_deadline',default=None)


class JobError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class JobView:
    job_id: UUID
    kind: str
    state: str
    body: bytes
    cancel_requested: bool
    recipient_principal: UUID | None
    recipient_role: str | None = None
    human_review: bool = False


@dataclass(frozen=True)
class JobReceipt:
    job_id: UUID
    state: str
    reason: str
    event_id: UUID | None = None


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    attempt_id: UUID
    attempt_number: int
    fence: int
    holder: UUID
    event_id: UUID | None = None


@dataclass(frozen=True)
class BudgetClaim:
    claim: Claim
    budget_status: str = 'not_enforced'


def _append_result(connection, scope, attempt_id, outcome, payload_id):
    connection.execute('''INSERT INTO coordination.job_results
        (tenant_id,project_id,attempt_id,outcome,payload_ref) VALUES (%s,%s,%s,%s,%s)''',
        (*_scope(scope),attempt_id,outcome,payload_id))


def _typed(data):
    if data['type'] == 'claim':
        return Claim(UUID(data['job_id']),UUID(data['attempt_id']),data['attempt_number'],data['fence'],UUID(data['holder']),UUID(data['event_id']) if data.get('event_id') else None)
    return JobReceipt(UUID(data['job_id']),data['state'],data['reason'],UUID(data['event_id']) if data.get('event_id') else None)


def _result(job_id, state, reason):
    return dict(type='job',job_id=str(job_id),state=state,reason=reason)


def _bytes(value):
    if not isinstance(value,bytes) or len(value) > MAX_PAYLOAD_BYTES:
        raise JobError('invalid_input')


def _fence(attempt_id, fence):
    if not isinstance(attempt_id,UUID) or type(fence) is not int or not 1 <= fence <= 2**63-1:
        raise JobError('invalid_input')


class Jobs:
    def __init__(self, connection, credential, installation_id, project_id):
        self.connection = connection
        self.credential = credential
        self.installation_id = installation_id
        self.project_id = project_id

    def _auth(self, action):
        return authorized(self.connection,self.credential,self.installation_id,self.project_id,action)

    def _control(self, scope):
        resolved = _resolve(self.connection,(hashlib.sha256(self.credential).hexdigest(),
            self.installation_id,self.project_id,'control'))
        if (resolved.tenant_id,resolved.project_id,resolved.principal_id) != (*_scope(scope),scope.principal_id):
            raise AuthError('forbidden')

    def _execute(self, operation, job_id, key, arguments, callback, control=False):
        token = _LEASE_DEADLINE.set(None)
        try:
            try:
                _validate(job_id,0,key)
                digest = hashlib.sha256(json.dumps([operation,str(job_id),arguments],separators=(',',':')).encode()).hexdigest()
                request_json = json.dumps([operation,str(job_id),arguments],separators=(',',':'))
                with self._auth('write') as scope:
                    if control:
                        self._control(scope)
                    saved = _request(self.connection,scope,key,digest)
                    if operation in ('job.claim','job.complete','job.return','job.release','job.abandon',
                                     'job.fail','job.accept','job.rework'):
                        # Keep request-key -> job lock ordering, and recheck identity
                        # even when a historical receipt will be replayed.
                        row = self._load(scope,job_id,True)
                        self._identity_policy(scope,self._metadata(scope,row),
                                              review=operation in ('job.accept','job.rework'))
                    if saved is None:
                        try:
                            saved = callback(scope)
                            saved['event_id'] = str(self.connection.execute(
                                'SELECT coordination.capture_job(%s)', (job_id,)).fetchone()[0])
                            saved['request_json'] = request_json
                            _save_request(self.connection,scope,key,digest,saved)
                        except psycopg.errors.UniqueViolation:
                            raise JobError('conflict') from None
                    self._accept_deadline()
                    if saved.get('request_json') != request_json:
                        raise JobError('conflict')
                    result = _typed(saved)
                return result
            except RecordError as error:
                raise JobError(error.code) from None
        finally:
            _LEASE_DEADLINE.reset(token)

    def _accept_deadline(self):
        # Final acceptance point, after all risky writes. Terminal state deliberately
        # expires the durable lease, so compare its originally verified deadline.
        deadline = _LEASE_DEADLINE.get()
        if deadline is not None and not self.connection.execute(
                'SELECT %s::timestamptz>clock_timestamp()', (deadline,)).fetchone()[0]:
            raise JobError('conflict')

    def _identity_policy(self, scope, meta, review=False):
        role = meta.get('recipient_role')
        human = meta.get('human_review',False)
        if (review and human) or (not review and role is not None):
            actor = lookup_bound(self.connection,scope.principal_id)
            if actor is None or not actor.adopted:
                raise JobError('forbidden')
            if review:
                if actor.kind != 'human':
                    raise JobError('forbidden')
            elif role not in actor.roles:
                raise JobError('forbidden')

    def _load(self, scope, job_id, lock=False):
        row = self.connection.execute('''SELECT id,kind,payload_ref,state,cancel_requested FROM coordination.jobs
            WHERE tenant_id=%s AND project_id=%s AND id=%s'''+(' FOR UPDATE' if lock else ''),
            (*_scope(scope),job_id)).fetchone()
        if row is None:
            raise JobError('not_found')
        return row

    def _metadata(self, scope, row):
        raw = self.connection.execute('SELECT body FROM core.payloads WHERE tenant_id=%s AND project_id=%s AND id=%s',
            (*_scope(scope),row[2])).fetchone()[0]
        value = json.loads(raw)
        if value.get('version') != 1:
            raise JobError('unavailable')
        return value

    def _view(self, scope, row):
        meta = self._metadata(scope,row)
        body = self.connection.execute('SELECT body FROM core.payloads WHERE tenant_id=%s AND project_id=%s AND id=%s',
            (*_scope(scope),UUID(meta['body_payload']))).fetchone()[0]
        recipient = None if meta['recipient'] is None else UUID(meta['recipient'])
        return JobView(row[0],row[1],row[3],body,row[4],recipient,
                       meta.get('recipient_role'),meta.get('human_review',False))

    def create(self, job_id, kind, body, request_key, recipient_principal=None, *,
               recipient_role=None, human_review=False):
        _bytes(body)
        if (not isinstance(kind,str) or re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}',kind) is None
                or (recipient_principal is not None and not isinstance(recipient_principal,UUID))
                or (recipient_role is not None and (not isinstance(recipient_role,str)
                    or re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}',recipient_role) is None))
                or type(human_review) is not bool or (human_review and kind != 'handoff')):
            raise JobError('invalid_input')
        def create(scope):
            body_id,_ = _payload(self.connection,scope,body)
            meta = dict(version=1,body_payload=str(body_id),creator=str(scope.principal_id),
                recipient=None if recipient_principal is None else str(recipient_principal),review=None,
                recipient_role=recipient_role,human_review=human_review)
            intent_id,_ = _payload(self.connection,scope,json.dumps(meta,separators=(',',':')).encode())
            dedupe = hashlib.sha256(json.dumps([str(scope.principal_id),request_key],separators=(',',':')).encode()).hexdigest()
            self.connection.execute('''INSERT INTO coordination.jobs
                (tenant_id,project_id,id,kind,payload_ref,idempotency_key) VALUES (%s,%s,%s,%s,%s,%s)''',
                (*_scope(scope),job_id,kind,intent_id,dedupe))
            return _result(job_id,'pending','created')
        arguments = [kind,hashlib.sha256(body).hexdigest(),
                     None if recipient_principal is None else str(recipient_principal)]
        if recipient_role is not None or human_review:
            arguments.append(dict(recipient_role=recipient_role,human_review=human_review))
        return self._execute('job.create',job_id,request_key,arguments,create,control=True)

    def get(self, job_id):
        if not isinstance(job_id,UUID):
            raise JobError('invalid_input')
        with self._auth('read') as scope:
            try:
                result = self._view(scope,self._load(scope,job_id))
            except JobError as error:
                if error.code != 'not_found':
                    raise
                result = None
        return result

    def list(self, limit=50, after=None):
        if type(limit) is not int or not 1 <= limit <= 100 or (after is not None and not isinstance(after,UUID)):
            raise JobError('invalid_input')
        with self._auth('read') as scope:
            rows = self.connection.execute('''SELECT id,kind,payload_ref,state,cancel_requested FROM coordination.jobs
                WHERE tenant_id=%s AND project_id=%s AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s''',
                (*_scope(scope),after,after,limit)).fetchall()
            result = [self._view(scope,row) for row in rows]
        return result

    def claim(self, job_id, request_key, ttl_seconds=60):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise JobError('invalid_input')
        def claim(scope):
            row = self._load(scope,job_id,True)
            if row[3] != 'pending' or row[4]:
                raise JobError('conflict')
            recipient = self._metadata(scope,row)['recipient']
            if recipient is not None and recipient != str(scope.principal_id):
                raise JobError('forbidden')
            number = self.connection.execute('''SELECT COALESCE(max(attempt_number),0)+1 FROM coordination.job_attempts
                WHERE tenant_id=%s AND project_id=%s AND job_id=%s''',(*_scope(scope),job_id)).fetchone()[0]
            previous = self.connection.execute('''SELECT fence FROM coordination.leases
                WHERE tenant_id=%s AND project_id=%s AND kind='job' AND resource_id=%s''',(*_scope(scope),job_id)).fetchone()
            fence = 1 if previous is None else previous[0]+1
            attempt = uuid4()
            self.connection.execute('''INSERT INTO coordination.leases
                (tenant_id,project_id,kind,resource_id,holder,fence,expires_at)
                VALUES (%s,%s,'job',%s,%s,%s,clock_timestamp()+make_interval(secs=>%s))
                ON CONFLICT (tenant_id,project_id,kind,resource_id) DO UPDATE
                SET holder=EXCLUDED.holder,fence=EXCLUDED.fence,expires_at=EXCLUDED.expires_at''',
                (*_scope(scope),job_id,str(scope.principal_id),fence,ttl_seconds))
            deadline = self.connection.execute('''SELECT expires_at FROM coordination.leases
                WHERE tenant_id=%s AND project_id=%s AND kind='job' AND resource_id=%s''',
                (*_scope(scope),job_id)).fetchone()[0]
            _LEASE_DEADLINE.set(deadline)
            self.connection.execute('''INSERT INTO coordination.job_attempts
                (tenant_id,project_id,id,job_id,attempt_number,fence,worker_id) VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                (*_scope(scope),attempt,job_id,number,fence,str(scope.principal_id)))
            self._state(scope,job_id,'running')
            return dict(type='claim',job_id=str(job_id),attempt_id=str(attempt),attempt_number=number,fence=fence,holder=str(scope.principal_id))
        return self._execute('job.claim',job_id,request_key,[ttl_seconds],claim)

    def claim_with_budget(self, job_id, request_key, ttl_seconds=60, budget=None):
        # Telemetry never changes eligibility or implements a budget reservation.
        return BudgetClaim(self.claim(job_id,request_key,ttl_seconds))

    def _state(self, scope, job_id, state, canceled=False):
        self.connection.execute('''UPDATE coordination.jobs SET state=%s,cancel_requested=%s
            WHERE tenant_id=%s AND project_id=%s AND id=%s''',(state,canceled,*_scope(scope),job_id))
        if state != 'running':
            self.connection.execute('''UPDATE coordination.leases SET expires_at=clock_timestamp()
                WHERE tenant_id=%s AND project_id=%s AND kind='job' AND resource_id=%s''',(*_scope(scope),job_id))

    def _attempt(self, scope, job_id):
        return self.connection.execute('''SELECT a.id,a.fence,a.worker_id,l.expires_at>clock_timestamp(),l.expires_at
            FROM coordination.job_attempts a JOIN coordination.leases l
            ON (l.tenant_id,l.project_id,l.resource_id,l.kind)=(a.tenant_id,a.project_id,a.job_id,'job')
            AND l.fence=a.fence AND l.holder=a.worker_id
            WHERE a.tenant_id=%s AND a.project_id=%s AND a.job_id=%s ORDER BY a.attempt_number DESC LIMIT 1''',
            (*_scope(scope),job_id)).fetchone()

    def _active(self, scope, row, attempt_id, fence, review=False):
        if not isinstance(attempt_id,UUID) or type(fence) is not int or fence < 1:
            raise JobError('invalid_input')
        current = self._attempt(scope,row[0])
        if row[3] != 'running' or row[4] or current is None or current[:2] != (attempt_id,fence) or not current[3]:
            raise JobError('conflict')
        _LEASE_DEADLINE.set(current[4])
        if (current[2] == str(scope.principal_id)) == review:
            raise JobError('forbidden')
        return current

    def _terminal(self, scope, row, attempt_id, state, body, reason):
        payload_id,_ = _payload(self.connection,scope,body)
        _append_result(self.connection,scope,attempt_id,state,payload_id)
        self._state(scope,row[0],state,state=='canceled')
        return _result(row[0],state,reason)

    def complete(self, job_id, attempt_id, fence, body, request_key):
        _fence(attempt_id,fence)
        _bytes(body)
        def complete(scope):
            row = self._load(scope,job_id,True)
            self._active(scope,row,attempt_id,fence)
            if row[1] == 'handoff':
                raise JobError('conflict')
            return self._terminal(scope,row,attempt_id,'succeeded',body,'completed')
        return self._execute('job.complete',job_id,request_key,[str(attempt_id),fence,hashlib.sha256(body).hexdigest()],complete)

    def cancel(self, job_id, request_key):
        def cancel(scope):
            row = self._load(scope,job_id,True)
            if row[3] not in ('pending','running'):
                raise JobError('conflict')
            attempt = self._attempt(scope,job_id)
            if attempt is None:
                self._state(scope,job_id,'canceled',True)
                return _result(job_id,'canceled','canceled')
            return self._terminal(scope,row,attempt[0],'canceled',b'owner cancellation','canceled')
        return self._execute('job.cancel',job_id,request_key,[],cancel,control=True)

    def expire(self, job_id, request_key):
        def expire(scope):
            row = self._load(scope,job_id,True)
            attempt = self._attempt(scope,job_id)
            if row[3] != 'running' or attempt is None or attempt[3]:
                raise JobError('conflict')
            return self._terminal(scope,row,attempt[0],'unresolved',b'lease expired; external effects unproven','expired_unproven')
        return self._execute('job.expire',job_id,request_key,[],expire,control=True)

    def _replace_metadata(self, scope, row, meta):
        meta = dict(meta,previous_intent=str(row[2]))
        payload_id,_ = _payload(self.connection,scope,json.dumps(meta,separators=(',',':')).encode())
        self.connection.execute('UPDATE coordination.jobs SET payload_ref=%s WHERE tenant_id=%s AND project_id=%s AND id=%s',
            (payload_id,*_scope(scope),row[0]))

    def retry(self, job_id, request_key):
        def retry(scope):
            row = self._load(scope,job_id,True)
            if row[3] not in ('failed','unresolved') or row[4]:
                raise JobError('conflict')
            meta = self._metadata(scope,row)
            if meta['review'] is not None:
                self._replace_metadata(scope,row,dict(meta,review=None))
            self._state(scope,job_id,'pending')
            return _result(job_id,'pending','explicit_retry')
        return self._execute('job.retry',job_id,request_key,[],retry,control=True)

    def _worker_terminal(self, operation, job_id, attempt_id, fence, body, key, state, reason):
        _fence(attempt_id,fence)
        _bytes(body)
        def finish(scope):
            row = self._load(scope,job_id,True)
            self._active(scope,row,attempt_id,fence)
            return self._terminal(scope,row,attempt_id,state,body,reason)
        return self._execute(operation,job_id,key,[str(attempt_id),fence,hashlib.sha256(body).hexdigest()],finish)

    def release(self, job_id, attempt_id, fence, request_key):
        return self._worker_terminal('job.release',job_id,attempt_id,fence,b'claim released; external effects unproven',request_key,'unresolved','released_unproven')

    def abandon(self, job_id, attempt_id, fence, request_key):
        return self._worker_terminal('job.abandon',job_id,attempt_id,fence,b'worker abandoned',request_key,'canceled','abandoned')

    def fail(self, job_id, attempt_id, fence, body, request_key):
        return self._worker_terminal('job.fail',job_id,attempt_id,fence,body,request_key,'failed','failed')

    def return_result(self, job_id, attempt_id, fence, body, request_key):
        _fence(attempt_id,fence)
        _bytes(body)
        def returned(scope):
            row = self._load(scope,job_id,True)
            self._active(scope,row,attempt_id,fence)
            meta = self._metadata(scope,row)
            if row[1] != 'handoff' or meta['review'] is not None:
                raise JobError('conflict')
            payload_id,_ = _payload(self.connection,scope,body)
            review = dict(payload=str(payload_id),attempt=str(attempt_id),fence=fence,holder=str(scope.principal_id))
            self._replace_metadata(scope,row,dict(meta,review=review))
            return _result(job_id,'running','returned')
        return self._execute('job.return',job_id,request_key,[str(attempt_id),fence,hashlib.sha256(body).hexdigest()],returned)

    def _review(self, operation, job_id, attempt_id, fence, request_key, state):
        _fence(attempt_id,fence)
        def review(scope):
            row = self._load(scope,job_id,True)
            current = self._active(scope,row,attempt_id,fence,review=True)
            returned = self._metadata(scope,row)['review']
            if row[1] != 'handoff' or returned is None or (returned['attempt'],returned['fence'],returned['holder']) != (str(attempt_id),fence,current[2]):
                raise JobError('conflict')
            payload_id = UUID(returned['payload'])
            _append_result(self.connection,scope,attempt_id,state,payload_id)
            self._state(scope,job_id,state)
            return _result(job_id,state,'accepted' if state=='succeeded' else 'rework')
        return self._execute(operation,job_id,request_key,[str(attempt_id),fence],review,control=True)

    def accept(self, job_id, attempt_id, fence, request_key):
        return self._review('job.accept',job_id,attempt_id,fence,request_key,'succeeded')

    def rework(self, job_id, attempt_id, fence, request_key):
        return self._review('job.rework',job_id,attempt_id,fence,request_key,'failed')
