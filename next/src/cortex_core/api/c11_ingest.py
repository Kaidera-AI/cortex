"""Scoped R010/R011/R012 adapters over one C05/C06 canonical commit path."""

import asyncio
import json
from uuid import NAMESPACE_URL, uuid5

from cortex_core.auth import AuthError
from cortex_core.records import RecordError, Records
from .c11a import C05Committed
from .c11b import C11bRecordPort, _credential


_FIELDS = {
    'knowledge': ({'content', 'source_file'},
                  {'content', 'source_file', 'category', 'section', 'on_conflict'}),
    'lesson': ({'summary'},
               {'summary', 'detail', 'category', 'importance', 'agent_name', 'on_conflict'}),
    'decision': ({'summary'},
                 {'summary', 'rationale', 'category', 'agent_name', 'on_conflict'}),
}


def _string(value, *, required=False, max_length=4096):
    if value is None and not required:
        return None
    if (not isinstance(value, str) or (required and not value.strip())
            or len(value) > max_length):
        raise RecordError('invalid_input')
    return value


def _body(kind, scope):
    raw = scope.get('_c11b_body')
    required, allowed = _FIELDS[kind]
    if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - allowed:
        raise RecordError('invalid_input')
    mode = 'conflict' if raw.get('on_conflict') is None else raw['on_conflict']
    if not isinstance(mode, str) or mode not in {'conflict', 'update'}:
        raise RecordError('invalid_input')
    category = _string(raw.get('category'), max_length=512)
    if kind == 'knowledge':
        content = _string(raw['content'], required=True, max_length=16 * 1024 * 1024)
        source = _string(raw['source_file'], required=True, max_length=4096)
        canonical = {'content': content, 'source_file': source,
                     'category': category, 'section': _string(raw.get('section'), max_length=4096)}
        identity = [source]
        writer = None
    elif kind == 'lesson':
        summary = _string(raw['summary'], required=True, max_length=4096)
        writer = _string(raw.get('agent_name'), max_length=256)
        importance = raw.get('importance', 5)
        if importance is not None and (type(importance) is not int or not -2**31 <= importance < 2**31):
            raise RecordError('invalid_input')
        canonical = {'summary': summary, 'detail': _string(raw.get('detail'), max_length=16 * 1024 * 1024),
                     'category': category, 'importance': importance, 'agent_name': writer}
        identity = [summary, category or '']
    else:
        summary = _string(raw['summary'], required=True, max_length=4096)
        writer = _string(raw.get('agent_name'), max_length=256)
        canonical = {'summary': summary, 'rationale': _string(raw.get('rationale'), max_length=16 * 1024 * 1024),
                     'category': category, 'agent_name': writer}
        identity = [summary, category or '']
    payload = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    if len(payload) > 16 * 1024 * 1024:
        raise RecordError('invalid_input')
    return identity, payload, mode, writer


def _response(result):
    status = result.status
    record_id = str(result.record_id)
    return {'id': record_id, 'status': status,
            'created': status == 'created', 'updated': status == 'updated',
            'embedded': False,
            'receipt': {'record_id': record_id, 'revision': result.revision,
                        'event_id': str(result.event_id) if result.event_id else None}}


class C11IngestPort(C11bRecordPort):
    """Configured project routing; principal and write scope are freshly verified."""

    def _ingest(self, kind, principal, scope, request_key):
        project = self._project(scope)
        credential = _credential(scope)
        identity, payload, mode, writer = _body(kind, scope)
        with self.connection_factory() as db:
            def check_writer(connection, verified, _record_id, _kind):
                if writer is not None:
                    row = connection.execute('SELECT coordination.c11b_writer_matches(%s)',
                                             (writer,)).fetchone()
                    if row is None or row[0] is not True:
                        raise AuthError('forbidden')
                if (str(verified.principal_id) != principal['principal_id']
                        or str(verified.project_id) != principal['project_id']
                        or str(verified.tenant_id) != tenant):
                    raise AuthError('forbidden')

            # This identity is only a stable record key; it grants no scope.
            # C05 reauthorizes the credential and runs the writer check in its
            # commit transaction, including exact-key replays and no-ops.
            tenant = principal.get('tenant_id')
            if not isinstance(tenant, str) or not tenant:
                raise AuthError('forbidden')
            identity_json = json.dumps([tenant, principal['project_id'], kind, identity],
                                       separators=(',', ':'), ensure_ascii=False)
            record_id = uuid5(NAMESPACE_URL, 'cortex.ingest:' + identity_json)
            result = Records(db, credential, self.installation_id, project,
                             before_commit=check_writer).ingest(record_id, kind, payload,
                                                                 mode, request_key)
        response = _response(result)
        return C05Committed(request_key, response['receipt'], True, response)

    async def ingest_knowledge(self, principal, scope, request_key):
        return await asyncio.to_thread(self._ingest, 'knowledge', principal, scope, request_key)

    async def ingest_lesson(self, principal, scope, request_key):
        return await asyncio.to_thread(self._ingest, 'lesson', principal, scope, request_key)

    async def ingest_decision(self, principal, scope, request_key):
        return await asyncio.to_thread(self._ingest, 'decision', principal, scope, request_key)
