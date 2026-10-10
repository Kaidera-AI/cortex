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


def _request(connection, scope, key, digest):
    _lock(connection,'request',(*_scope(scope),scope.principal_id,key))
    row = connection.execute('''SELECT request_sha256,outcome,receipt FROM coordination.idempotency
        WHERE tenant_id=%s AND project_id=%s AND principal_id=%s AND request_key=%s''',
        (*_scope(scope),scope.principal_id,key)).fetchone()
    if row is None:
        return None
    if row[0] != digest or row[1] != 'committed':
        raise RecordError('conflict')
    return row[2]


def _save_request(connection, scope, key, digest, receipt):
    connection.execute('''INSERT INTO coordination.idempotency
        (tenant_id,project_id,principal_id,request_key,request_sha256,outcome,receipt)
        VALUES (%s,%s,%s,%s,%s,'committed',%s)''',
        (*_scope(scope),scope.principal_id,key,digest,Jsonb(receipt)))


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


class Records:
    """Own each private request transaction; no result escapes before auth commit."""
    def __init__(self, connection, credential, installation_id, project_id):
        self.connection = connection
        self.credential = credential
        self.installation_id = installation_id
        self.project_id = project_id

    def _authorized(self, action):
        return authorized(self.connection,self.credential,self.installation_id,self.project_id,action)

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

    def _mutate(self, operation, record_id, kind, body, expected, key):
        digest = hashlib.sha256(json.dumps([operation,str(record_id),kind,
            None if body is None else hashlib.sha256(body).hexdigest(),expected],separators=(',',':')).encode()).hexdigest()
        with self._authorized('write') as scope:
            saved = _request(self.connection,scope,key,digest)
            if saved is not None:
                result = _receipt(saved)
            else:
                _lock(self.connection,'record',(*_scope(scope),record_id))
                row = self.connection.execute('''SELECT kind,current_revision FROM core.records
                    WHERE tenant_id=%s AND project_id=%s AND id=%s FOR UPDATE''',(*_scope(scope),record_id)).fetchone()
                if (row is None and (expected != 0 or operation == 'delete')) or (row is not None and
                        (row[1] != expected or (operation == 'put' and row[0] != kind))):
                    raise RecordError('conflict')
                revision = expected + 1
                tombstone = operation == 'delete'
                if tombstone:
                    payload_id,payload_digest = self.connection.execute('''SELECT p.id,p.sha256
                        FROM core.record_revisions v JOIN core.payloads p
                        ON (p.tenant_id,p.project_id,p.id)=(v.tenant_id,v.project_id,v.payload_ref)
                        WHERE v.tenant_id=%s AND v.project_id=%s AND v.record_id=%s AND v.revision=%s''',
                        (*_scope(scope),record_id,expected)).fetchone()
                else:
                    payload_id,payload_digest = _payload(self.connection,scope,body)
                try:
                    if row is None:
                        self.connection.execute('''INSERT INTO core.records
                            (tenant_id,project_id,id,kind,current_revision,tombstone) VALUES (%s,%s,%s,%s,%s,%s)''',
                            (*_scope(scope),record_id,kind,revision,tombstone))
                    else:
                        self.connection.execute('''UPDATE core.records SET current_revision=%s,tombstone=%s
                            WHERE tenant_id=%s AND project_id=%s AND id=%s''',
                            (revision,tombstone,*_scope(scope),record_id))
                    _append_revision(self.connection,scope,record_id,revision,payload_id,tombstone)
                    data = dict(record_id=str(record_id),revision=revision,tombstone=tombstone,payload_sha256=payload_digest)
                    event = self.connection.execute('''SELECT event_id FROM coordination.outbox
                        WHERE tenant_id=%s AND project_id=%s AND aggregate_id=%s AND aggregate_revision=%s''',
                        (*_scope(scope),record_id,revision)).fetchone()
                    if event is None:
                        raise RecordError('core_unavailable')
                    data['event_id'] = str(event[0])
                    _save_request(self.connection,scope,key,digest,data)
                    result = _receipt(data)
                except psycopg.errors.UniqueViolation:
                    raise RecordError('conflict') from None
        return result
