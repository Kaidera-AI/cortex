"""Private canonical record port; event-bearing writer admission belongs to C06."""
from dataclasses import dataclass
import hashlib
import json
import re
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from .auth import authorized

MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class RecordError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MutationReceipt:
    record_id: UUID
    revision: int
    tombstone: bool
    payload_sha256: str


@dataclass(frozen=True)
class EventMutationReceipt(MutationReceipt):
    """C06 private receipt extends the frozen C05 eventless transport shape."""
    event_id: UUID


@dataclass(frozen=True)
class IngestReceipt:
    record_id: UUID
    revision: int
    event_id: UUID | None
    status: str


@dataclass(frozen=True)
class Record:
    record_id: UUID
    kind: str
    revision: int
    tombstone: bool
    payload_id: UUID
    body: bytes
    payload_sha256: str


def _lock(connection, domain, values):
    # Hash collisions only serialize unrelated requests, never grant authority.
    raw = hashlib.sha256(json.dumps([domain,*map(str,values)],separators=(',',':')).encode()).digest()
    connection.execute('SELECT pg_advisory_xact_lock(%s,%s)',
                       (int.from_bytes(raw[:4],'big',signed=True),int.from_bytes(raw[4:8],'big',signed=True)))


def _scope(scope):
    return (scope.tenant_id,scope.project_id)


def _append_revision(connection, scope, record_id, revision, payload_id, tombstone):
    connection.execute('''INSERT INTO core.record_revisions
        (tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES (%s,%s,%s,%s,%s,%s)''',
        (*_scope(scope),record_id,revision,payload_id,tombstone))


def _payload(connection, scope, body):
    payload_id = uuid4()
    digest = hashlib.sha256(body).hexdigest()
    connection.execute('INSERT INTO core.payloads (tenant_id,project_id,id,body,sha256) VALUES (%s,%s,%s,%s,%s)',
                       (*_scope(scope),payload_id,body,digest))
    return payload_id,digest


def _private(connection, query, arguments):
    # Map private decision conflicts before authorized() maps dependency errors.
    try:
        return connection.execute(query, arguments)
    except psycopg.errors.UniqueViolation:
        raise RecordError('conflict') from None


def _request(connection, scope, key, digest):
    _lock(connection,'request',(*_scope(scope),scope.principal_id,key))
    row = _private(connection,'SELECT * FROM coordination.c05_request(%s,%s)',(key,digest)).fetchone()
    if row is None:
        return None
    if row[0] != digest or row[1] != 'committed':
        raise RecordError('conflict')
    return row[2]


def _save_request(connection, scope, key, digest, receipt, outcome='committed'):
    connection.execute('''INSERT INTO coordination.idempotency
        (tenant_id,project_id,principal_id,request_key,request_sha256,outcome,receipt)
        VALUES (%s,%s,%s,%s,%s,%s,%s)''',
        (*_scope(scope),scope.principal_id,key,digest,outcome,Jsonb(receipt)))


def _validate(record_id, revision, key):
    if (not isinstance(record_id,UUID) or type(revision) is not int or not 0 <= revision < 2**63-1
            or not isinstance(key,str) or not 1 <= len(key) <= 256 or '\x00' in key):
        raise RecordError('invalid_input')
    try:
        key.encode('utf-8')
    except UnicodeError:
        raise RecordError('invalid_input') from None


def _receipt(data):
    return EventMutationReceipt(UUID(data['record_id']),data['revision'],data['tombstone'],data['payload_sha256'],UUID(data['event_id']) if data.get('event_id') else None)


def _ingest_receipt(data):
    if (not isinstance(data, dict) or data.get('status') not in {'created', 'updated', 'unchanged'}
            or type(data.get('revision')) is not int or data['revision'] < 1
            or data.get('status') == 'unchanged' and data.get('event_id') is not None
            or data.get('status') != 'unchanged' and not data.get('event_id')):
        raise RecordError('core_unavailable')
    try:
        return IngestReceipt(UUID(data['record_id']), data['revision'],
                             UUID(data['event_id']) if data['event_id'] else None,
                             data['status'])
    except (ValueError, TypeError, KeyError):
        raise RecordError('core_unavailable') from None


class Records:
    """Own each private request transaction; no result escapes before auth commit."""
    def __init__(self, connection, credential, installation_id, project_id,
                 before_commit=None):
        if before_commit is not None and not callable(before_commit):
            raise RecordError('invalid_input')
        self.connection = connection
        self.credential = credential
        self.installation_id = installation_id
        self.project_id = project_id
        self.before_commit = before_commit

    def _authorized(self, action):
        return authorized(self.connection,self.credential,self.installation_id,self.project_id,action)

    def lookup_request(self, request_key):
        """Return only this principal's committed C05 write for exact API replay."""
        if not isinstance(request_key,str) or not 1 <= len(request_key) <= 256:
            raise RecordError('invalid_input')
        with self._authorized('write'):
            row = _private(self.connection,
                'SELECT * FROM coordination.c11b_request(%s)',(request_key,)).fetchone()
            result = None if row is None else (row[0],row[1])
        return result

    def get(self, record_id, include_tombstone=False):
        if not isinstance(record_id,UUID) or type(include_tombstone) is not bool:
            raise RecordError('invalid_input')
        with self._authorized('read') as scope:
            row = self.connection.execute('''SELECT r.kind,r.current_revision,r.tombstone,p.id,p.body,p.sha256
                FROM core.records r JOIN core.record_revisions v
                ON (v.tenant_id,v.project_id,v.record_id,v.revision)=(r.tenant_id,r.project_id,r.id,r.current_revision)
                JOIN core.payloads p ON (p.tenant_id,p.project_id,p.id)=(v.tenant_id,v.project_id,v.payload_ref)
                WHERE r.tenant_id=%s AND r.project_id=%s AND r.id=%s''',(*_scope(scope),record_id)).fetchone()
            result = None if row is None or (row[2] and not include_tombstone) else Record(record_id,*row)
        return result

    def put(self, record_id, kind, body, expected_revision, request_key):
        _validate(record_id,expected_revision,request_key)
        if not isinstance(kind,str) or re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}',kind) is None or not isinstance(body,bytes) or len(body) > MAX_PAYLOAD_BYTES or kind == 'core' or kind.startswith('core.'):
            raise RecordError('invalid_input')
        return self._mutate('put',record_id,kind,body,expected_revision,request_key)

    def delete(self, record_id, expected_revision, request_key):
        _validate(record_id,expected_revision,request_key)
        if expected_revision == 0:
            raise RecordError('invalid_input')
        return self._mutate('delete',record_id,None,None,expected_revision,request_key)

    def ingest(self, record_id, kind, body, on_conflict, request_key):
        """Commit one legacy ingest identity, including a durable no-op outcome."""
        _validate(record_id, 0, request_key)
        if (kind not in {'knowledge', 'lesson', 'decision'}
                or not isinstance(body, bytes) or len(body) > MAX_PAYLOAD_BYTES
                or on_conflict not in {'conflict', 'update'}):
            raise RecordError('invalid_input')
        payload_digest = hashlib.sha256(body).hexdigest()
        with self._authorized('write') as scope:
            _lock(self.connection, 'request', (*_scope(scope), scope.principal_id, request_key))
            row = _private(self.connection,
                'SELECT coordination.c11_ingest_request(%s)', (request_key,)).fetchone()
            saved = row[0] if row is not None else None
            if saved is not None:
                if (saved.get('record_id') != str(record_id)
                        or saved.get('ingest_kind') != kind
                        or saved.get('payload_sha256') != payload_digest
                        or saved.get('ingest_mode') != on_conflict):
                    raise RecordError('conflict')
                result = _ingest_receipt(saved)
            else:
                _lock(self.connection, 'record', (*_scope(scope), record_id))
                head = _private(self.connection,
                    'SELECT * FROM coordination.c11_ingest_head(%s,%s)',
                    (record_id, kind)).fetchone()
                if head is not None and head[1]:
                    raise RecordError('conflict')
                if head is not None and head[2] == payload_digest:
                    status, revision, event_id = 'unchanged', head[0], None
                    request = ['ingest.observe', str(record_id), kind,
                               payload_digest, revision, on_conflict]
                    outcome = 'observed'
                else:
                    if head is not None and on_conflict != 'update':
                        raise RecordError('conflict')
                    status = 'created' if head is None else 'updated'
                    revision = 1 if head is None else head[0] + 1
                    request = ['put', str(record_id), kind, payload_digest, revision - 1]
                    outcome = 'committed'
                    payload_id, _ = _payload(self.connection, scope, body)
                    if head is None:
                        try:
                            self.connection.execute('''INSERT INTO core.records
                                (tenant_id,project_id,id,kind,current_revision,tombstone)
                                VALUES (%s,%s,%s,%s,%s,false)''',
                                (*_scope(scope), record_id, kind, revision))
                        except psycopg.errors.UniqueViolation:
                            raise RecordError('conflict') from None
                    else:
                        _private(self.connection,
                            'SELECT coordination.c05_update_head(%s,%s,%s,false)',
                            (record_id, head[0], revision))
                    _append_revision(self.connection, scope, record_id, revision,
                                     payload_id, False)
                    row = _private(self.connection,
                        'SELECT coordination.c06_record_event(%s,%s)',
                        (record_id, revision)).fetchone()
                    if row is None or row[0] is None:
                        raise RecordError('core_unavailable')
                    event_id = str(row[0])
                request_json = json.dumps(request, separators=(',', ':'))
                digest = hashlib.sha256(request_json.encode()).hexdigest()
                data = {'record_id': str(record_id), 'revision': revision,
                        'event_id': event_id, 'status': status, 'tombstone': False,
                        'payload_sha256': payload_digest, 'ingest_kind': kind,
                        'ingest_mode': on_conflict,
                        'request_json': request_json}
                _save_request(self.connection, scope, request_key, digest, data, outcome)
                result = _ingest_receipt(data)
            if self.before_commit is not None:
                self.before_commit(self.connection, scope, record_id, kind)
        return result

    def _mutate(self, operation, record_id, kind, body, expected, key):
        digest = hashlib.sha256(json.dumps([operation,str(record_id),kind,
            None if body is None else hashlib.sha256(body).hexdigest(),expected],separators=(',',':')).encode()).hexdigest()
        request_json = json.dumps([operation,str(record_id),kind,None if body is None else hashlib.sha256(body).hexdigest(),expected],separators=(',',':'))
        with self._authorized('write') as scope:
            saved = _request(self.connection,scope,key,digest)
            if saved is not None:
                if saved.get('request_json') != request_json:
                    raise RecordError('conflict')
                result = _receipt(saved)
            else:
                _lock(self.connection,'record',(*_scope(scope),record_id))
                row = _private(self.connection,'SELECT * FROM coordination.c05_record_head(%s,%s,%s,%s)',
                    (record_id,operation,kind,int(expected))).fetchone()
                if (row is None and (expected != 0 or operation == 'delete')) or (row is not None and
                        (row[1] != expected or (operation == 'put' and row[0] != kind))):
                    raise RecordError('conflict')
                revision = expected + 1
                tombstone = operation == 'delete'
                if tombstone:
                    payload_id,payload_digest = _private(self.connection,
                        'SELECT * FROM coordination.c05_record_payload(%s,%s)',(record_id,int(expected))).fetchone()
                else:
                    payload_id,payload_digest = _payload(self.connection,scope,body)
                try:
                    if row is None:
                        self.connection.execute('''INSERT INTO core.records
                            (tenant_id,project_id,id,kind,current_revision,tombstone) VALUES (%s,%s,%s,%s,%s,%s)''',
                            (*_scope(scope),record_id,kind,revision,tombstone))
                    else:
                        _private(self.connection,'SELECT coordination.c05_update_head(%s,%s,%s,%s)',
                            (record_id,int(expected),revision,tombstone))
                    _append_revision(self.connection,scope,record_id,revision,payload_id,tombstone)
                    data = dict(record_id=str(record_id),revision=revision,tombstone=tombstone,payload_sha256=payload_digest)
                    event = _private(self.connection,'SELECT coordination.c06_record_event(%s,%s)',
                        (record_id,revision)).fetchone()
                    if event is None or event[0] is None:
                        raise RecordError('core_unavailable')
                    data['event_id'] = str(event[0])
                    data['request_json'] = request_json
                    if self.before_commit is not None:
                        self.before_commit(self.connection, scope, record_id, kind)
                    _save_request(self.connection,scope,key,digest,data)
                    result = _receipt(data)
                except psycopg.errors.UniqueViolation:
                    raise RecordError('conflict') from None
        return result
