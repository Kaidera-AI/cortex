"""Concrete released memory adapter over C04 admission and C05/C06 commits."""

import asyncio
import hashlib
import json
from uuid import UUID, NAMESPACE_URL, uuid5

from cortex_core.auth import AuthError, authorized
from cortex_core.records import RecordError, Records
from .c11a import C05Committed


def _headers(scope):
    return dict(scope.get('headers') or [])


def _credential(scope):
    value = _headers(scope).get(b'authorization', b'')
    if not value.startswith(b'Bearer ') or len(value) <= 7:
        raise AuthError('unauthenticated')
    return value[7:]


def _writer(scope):
    try:
        name = _headers(scope).get(b'x-agent-name', b'').decode('utf-8')
    except UnicodeError:
        raise AuthError('forbidden') from None
    if not name or len(name) > 256:
        raise AuthError('forbidden')
    return name


def _body(scope, writer):
    raw = scope.get('_c11b_body')
    if not isinstance(raw, dict) or set(raw) - {'section', 'content', 'category', 'source'}:
        raise RecordError('invalid_input')
    section, content = raw.get('section'), raw.get('content')
    category, source = raw.get('category', 'operational'), raw.get('source')
    if (not isinstance(section, str) or not isinstance(content, str)
            or not isinstance(category, str) or source is not None and not isinstance(source, str)):
        raise RecordError('invalid_input')
    source = source or f'manual:{writer}'
    if not source or len(source) > 512:
        raise RecordError('invalid_input')
    value = {'section': section, 'content': content, 'category': category, 'source': source}
    return source, json.dumps(value, sort_keys=True, separators=(',', ':'),
                              ensure_ascii=False).encode('utf-8')


def _record_id(scope, source):
    return uuid5(NAMESPACE_URL, f'cortex.memory:{scope.tenant_id}:{scope.project_id}:{source}')


def _legacy_response(receipt):
    created = receipt.revision == 1
    action = 'created' if created else 'updated'
    return {'id': str(receipt.record_id), 'action': action, 'status': action,
            'created': created, 'updated': not created, 'embedded': False}


class C11bRecordPort:
    """Server-configured project routing; every use reauthorizes the credential."""

    def __init__(self, connection_factory, installation_id, projects):
        if (not callable(connection_factory) or not isinstance(installation_id, UUID)
                or not isinstance(projects, dict)
                or any(not isinstance(k, str) or not isinstance(v, UUID)
                       for k, v in projects.items())):
            raise ValueError('C11b port configuration is invalid')
        self.connection_factory = connection_factory
        self.installation_id = installation_id
        self.projects = dict(projects)

    def _project(self, scope):
        try:
            key = _headers(scope).get(b'x-project', b'').decode('utf-8')
        except UnicodeError:
            raise AuthError('scope_mismatch') from None
        project = self.projects.get(key)
        if project is None:
            raise AuthError('scope_mismatch')
        return project

    def _principal(self, scope):
        project = self._project(scope)
        action = 'read' if scope.get('method') == 'GET' else 'write'
        with self.connection_factory() as db:
            with authorized(db, _credential(scope), self.installation_id, project, action) as identity:
                result = {'principal_id': str(identity.principal_id),
                          'project_id': str(identity.project_id)}
        return result

    async def principal(self, scope):
        return await asyncio.to_thread(self._principal, scope)

    def _write_memory(self, principal, scope, request_key):
        project = self._project(scope)
        credential = _credential(scope)
        writer = _writer(scope)
        with self.connection_factory() as db:
            with authorized(db, credential, self.installation_id, project, 'write') as identity:
                if (str(identity.principal_id) != principal['principal_id']
                        or str(identity.project_id) != principal['project_id']):
                    raise AuthError('forbidden')
                row = db.execute('SELECT coordination.c11b_writer_matches(%s)',
                                 (writer,)).fetchone()
                if row is None or row[0] is not True:
                    raise AuthError('forbidden')
                source, body = _body(scope, writer)
                record_id = _record_id(identity, source)
            records = Records(db, credential, self.installation_id, project)
            saved = records.lookup_request(request_key)
            digest = hashlib.sha256(body).hexdigest()
            if saved is not None:
                try:
                    operation, old_id, kind, old_digest, expected = json.loads(saved[0])
                except (TypeError, ValueError):
                    raise RecordError('conflict') from None
                if (operation, old_id, kind, old_digest) != ('put', str(record_id), 'memory', digest):
                    raise RecordError('conflict')
            else:
                current = records.get(record_id, include_tombstone=True)
                if current is not None and current.tombstone:
                    raise RecordError('conflict')
                expected = 0 if current is None else current.revision
            receipt = records.put(record_id, 'memory', body, expected, request_key)
        core_receipt = {'record_id': str(receipt.record_id), 'revision': receipt.revision,
                        'event_id': str(receipt.event_id),
                        'payload_sha256': receipt.payload_sha256}
        return C05Committed(request_key, core_receipt, True, _legacy_response(receipt))

    async def write_memory(self, principal, scope, request_key):
        return await asyncio.to_thread(self._write_memory, principal, scope, request_key)

    def _read_record(self, principal, scope, raw_id):
        try:
            record_id = UUID(raw_id)
        except ValueError:
            raise RecordError('invalid_input') from None
        project = self._project(scope)
        with self.connection_factory() as db:
            records = Records(db, _credential(scope), self.installation_id, project)
            value = records.get(record_id)
        if value is None:
            return None
        if value.kind != 'memory':
            raise RecordError('conflict')
        body = json.loads(value.body)
        return {'id': str(value.record_id), 'kind': value.kind, 'revision': value.revision,
                'section': body['section'], 'content': body['content'],
                'category': body['category'], 'source': body['source']}

    async def read_record(self, principal, scope, record_id):
        return await asyncio.to_thread(self._read_record, principal, scope, record_id)
