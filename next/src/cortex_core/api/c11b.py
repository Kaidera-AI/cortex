"""Concrete released memory adapter over C04 admission and C05/C06 commits."""

import asyncio
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID, NAMESPACE_URL, uuid5

from cortex_core.auth import AuthError, authorized
from cortex_core.records import RecordError, Records
from .c11a import C05Committed


_ISO_TIMESTAMP = (r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                  r'(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?')


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


def _session_body(scope):
    raw = scope.get('_c11b_body')
    allowed = {'session_uuid', 'agent', 'task', 'source_path', 'provider', 'cwd',
               'git_branch', 'source_kind', 'metadata', 'messages'}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise RecordError('invalid_input')
    try:
        session_id = UUID(str(raw.get('session_uuid')))
    except (ValueError, TypeError, AttributeError):
        raise RecordError('invalid_input') from None
    for key in ('agent', 'source_path', 'provider'):
        value = raw.get(key)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 512:
            raise RecordError('invalid_input')
    for key in ('task', 'cwd', 'git_branch', 'source_kind'):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 2048):
            raise RecordError('invalid_input')
    if raw.get('metadata') is not None and not isinstance(raw['metadata'], dict):
        raise RecordError('invalid_input')
    messages = raw.get('messages', [])
    if not isinstance(messages, list) or len(messages) > 10000:
        raise RecordError('invalid_input')
    translated = []
    roles = {'user': 'human', 'assistant': 'agent', 'human': 'human',
             'agent': 'agent', 'system': 'system'}
    for message in messages:
        if not isinstance(message, dict) or set(message) - {'role', 'content', 'ts', 'metadata'}:
            raise RecordError('invalid_input')
        role, content = message.get('role'), message.get('content')
        if (not isinstance(role, str) or role.lower() not in roles
                or not isinstance(content, str) or len(content) > 65536
                or message.get('ts') is not None and not isinstance(message['ts'], str)
                or message.get('metadata') is not None and not isinstance(message['metadata'], dict)):
            raise RecordError('invalid_input')
        timestamp = message.get('ts')
        if timestamp is not None:
            if not timestamp or len(timestamp) > 64:
                raise RecordError('invalid_input')
            if re.fullmatch(_ISO_TIMESTAMP, timestamp) is None:
                raise RecordError('invalid_input')
            try:
                datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            except ValueError:
                raise RecordError('invalid_input') from None
        translated.append({**message, 'role': roles[role.lower()]})
    canonical = {**raw, 'session_uuid': str(session_id), 'messages': translated}
    payload = json.dumps(canonical, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False).encode('utf-8')
    if len(payload) > 8 * 1024 * 1024:
        raise RecordError('invalid_input')
    return session_id, canonical, payload


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
        action = 'read' if scope.get('method') == 'GET' or scope.get('path') == '/search' else 'write'
        with self.connection_factory() as db:
            with authorized(db, _credential(scope), self.installation_id, project, action) as identity:
                result = {'principal_id': str(identity.principal_id),
                          'project_id': str(identity.project_id),
                          'tenant_id': str(identity.tenant_id),
                          'permission_generation': identity.permission_generation}
        return result

    async def principal(self, scope):
        return await asyncio.to_thread(self._principal, scope)

    def _credential_active(self, scope):
        digest = hashlib.sha256(_credential(scope)).hexdigest()
        with self.connection_factory() as db:
            value = db.execute('SELECT auth.credential_active(%s,%s)',
                               (digest, self.installation_id)).fetchone()
        if value is None or type(value[0]) is not bool:
            raise AuthError('core_unavailable')
        return value[0]

    async def credential_active(self, scope):
        return await asyncio.to_thread(self._credential_active, scope)

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

    def _ingest_session(self, principal, scope, request_key):
        project = self._project(scope)
        credential = _credential(scope)
        session_id, body, payload = _session_body(scope)
        with self.connection_factory() as db:
            with authorized(db, credential, self.installation_id, project, 'write') as identity:
                if (str(identity.principal_id) != principal['principal_id']
                        or str(identity.project_id) != principal['project_id']):
                    raise AuthError('forbidden')
                row = db.execute('SELECT coordination.c11b_writer_matches(%s)',
                                 (body['agent'],)).fetchone()
                if row is None or row[0] is not True:
                    raise AuthError('forbidden')
                record_id = uuid5(NAMESPACE_URL, f'cortex.session:{identity.tenant_id}:'
                                  f'{identity.project_id}:{session_id}')
            def claim_source(connection, _scope, saved_id, saved_kind):
                if saved_kind != 'session' or saved_id != record_id:
                    raise RecordError('conflict')
                connection.execute('SELECT coordination.c11b3_session_claim(%s,%s,%s)',
                                   (body['source_path'], session_id, record_id))
            records = Records(db, credential, self.installation_id, project,
                              before_commit=claim_source)
            saved = records.lookup_request(request_key)
            digest = hashlib.sha256(payload).hexdigest()
            if saved is not None:
                try:
                    operation, old_id, kind, old_digest, expected = json.loads(saved[0])
                except (TypeError, ValueError):
                    raise RecordError('conflict') from None
                if (operation, old_id, kind, old_digest) != ('put', str(record_id), 'session', digest):
                    raise RecordError('conflict')
            else:
                current = records.get(record_id, include_tombstone=True)
                if current is not None and current.tombstone:
                    raise RecordError('conflict')
                expected = 0 if current is None else current.revision
            receipt = records.put(record_id, 'session', payload, expected, request_key)
        response = {'session_id': str(session_id), 'agent_id': str(identity.principal_id),
                    'messages_inserted': len(body['messages'])}
        core_receipt = {'record_id': str(receipt.record_id), 'revision': receipt.revision,
                        'event_id': str(receipt.event_id), 'payload_sha256': receipt.payload_sha256}
        return C05Committed(request_key, core_receipt, True, response)

    async def ingest_session(self, principal, scope, request_key):
        return await asyncio.to_thread(self._ingest_session, principal, scope, request_key)

    def _read_record(self, principal, scope, raw_id):
        try:
            record_id = UUID(raw_id)
        except ValueError:
            raise RecordError('invalid_input') from None
        project = self._project(scope)
        with self.connection_factory() as db:
            records = Records(db, _credential(scope), self.installation_id, project)
            value = records.get(record_id, include_tombstone=True)
        if value is None:
            return None
        if value.tombstone:
            raise RecordError('gone')
        if value.kind != 'memory':
            return None
        body = json.loads(value.body)
        return {'id': str(value.record_id), 'kind': value.kind, 'revision': value.revision,
                'payload_sha256': value.payload_sha256,
                'section': body['section'], 'content': body['content'],
                'category': body['category'], 'source': body['source']}

    async def read_record(self, principal, scope, record_id):
        return await asyncio.to_thread(self._read_record, principal, scope, record_id)
