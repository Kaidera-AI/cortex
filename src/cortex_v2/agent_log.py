"""Scoped compatibility for the frozen agent log command, using v2 content."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import Field, StrictStr, ValidationError, field_validator

from .content import create_content, get_content
from .models import CreateContentRequest, StrictInput, _reject_nul
from .receipts import begin_command, commit_receipt, request_digest
from .store import ApiProblem

MARKER = 'cortex.agent-log.v1'
KINDS = {'decision': 'decision', 'lesson': 'lesson', 'team_event': 'progress'}


class LogRequest(StrictInput):
    event_type: Literal['commit', 'decision', 'lesson', 'started', 'stopped',
                        'blocked', 'unblocked', 'bug', 'handoff', 'question']
    summary: StrictStr = Field(min_length=1, max_length=65536)
    files_affected: list[StrictStr] | None = Field(default=None, max_length=1024)
    metadata: dict[str, Any] | None = None

    @field_validator('summary')
    @classmethod
    def summary_text(cls, value):
        return _reject_nul(value, 'summary')

    @field_validator('files_affected', 'metadata')
    @classmethod
    def metadata_text(cls, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 1024**2 or '\\u0000' in raw:
            raise ValueError('invalid log metadata')
        return value


def _refuse(status=422, code='invalid_log_request'):
    return ApiProblem(status, code, 'The selected log request is unavailable or mismatched.')


def _id(value):
    try:
        parsed = uuid.UUID(value)
        if str(parsed) != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise _refuse() from None


def _view(row, context, kind):
    payload = row.get('payload', {})
    if (kind not in KINDS or row.get('content_class') != KINDS[kind]
            or row.get('scope_id') != str(context.selected.scope_id)
            or row.get('revision') != 1 or payload.get('agent_log_schema') != MARKER
            or payload.get('summary') != row.get('body')
            or not isinstance(payload.get('agent_name'), str)
            or payload.get('project') != context.selected.alias
            or not isinstance(payload.get('files'), list)):
        raise _refuse(404, 'log_record_not_found')
    data = {'id': row['content_id'], 'project': payload['project'],
            'agent_name': payload['agent_name'], 'summary': row['body']}
    if kind == 'team_event':
        data.update(event_type=payload['event_type'], files=payload['files'], detail=payload['metadata'])
    else:
        data.update(category=None, metadata=payload['metadata'])
    return {'kind': kind, 'id': row['content_id'], 'row': data, 'verified': True}


async def read_log(connection, context, kind, write_id):
    if kind not in KINDS:
        raise _refuse()
    row = await get_content(connection, _id(write_id), 1, False)
    return _view(row, context, kind)


async def write_log(connection, context, payload, idempotency_key, agent_name):
    registered = await connection.fetchval('''
        SELECT p.principal_name FROM cortex_auth.principals p
          JOIN cortex_auth.actor_bindings b ON b.principal_id=p.principal_id
          JOIN cortex_auth.actors a ON a.actor_id=b.actor_id
          JOIN cortex_auth.memberships m ON m.actor_id=a.actor_id
         WHERE p.principal_id=$1 AND m.scope_id=$2 AND p.status='active'
           AND a.actor_kind='agent' AND m.status='active'
           AND m.membership_role IN ('owner','lead','member')
    ''', context.principal.principal_id, context.selected.scope_id)
    if (not isinstance(registered, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,95}', registered)
            or registered != agent_name):
        raise _refuse(403, 'agent_identity_mismatch')
    digest = request_digest({'request': payload.model_dump(mode='json'),
                             'scope_id': str(context.selected.scope_id), 'agent': registered})
    previous, replayed = await begin_command(connection, principal_id=context.principal.principal_id,
        operation='agent.log', idempotency_key=idempotency_key, digest=digest, scope_id=context.selected.scope_id)
    if replayed and previous is not None:
        return 200, previous, True
    common = {'agent_log_schema': MARKER, 'project': context.selected.alias,
              'agent_name': registered + '@' + context.selected.alias, 'summary': payload.summary,
              'event_type': payload.event_type, 'files': payload.files_affected or [], 'metadata': payload.metadata or {}}

    async def record(kind):
        fields = dict(common)
        if kind == 'decision':
            fields['statement'] = payload.summary
        elif kind == 'lesson':
            fields['lesson'] = payload.summary
        body = CreateContentRequest(content_class=KINDS[kind], payload=fields, body=payload.summary)
        key = hashlib.sha256((idempotency_key + ':' + kind).encode()).hexdigest()
        _, receipt, _ = await create_content(connection, context, body, key)
        if (receipt.get('state') != 'committed' or receipt.get('scope_id') != str(context.selected.scope_id)
                or receipt.get('revision') != 1):
            raise _refuse(500, 'log_write_unverified')
        row = await get_content(connection, _id(receipt['content_id']), 1, False)
        if (row.get('payload') != fields or row.get('body') != payload.summary
                or row.get('created_by_principal') != str(context.principal.principal_id)):
            raise _refuse(500, 'log_write_unverified')
        _view(row, context, kind)
        return receipt['content_id']

    if payload.event_type in ('decision', 'lesson'):
        own = await record(payload.event_type)
        event = await record('team_event')
        response = {'id': own, 'embedded': False, 'verified': True, 'team_event_id': event}
    else:
        event = await record('team_event')
        response = {'logged': True, 'id': event, 'event_type': payload.event_type, 'verified': True}
    await commit_receipt(connection, principal_id=context.principal.principal_id, operation='agent.log',
        idempotency_key=idempotency_key, digest=digest, receipt_kind='committed', receipt=response,
        scope_id=context.selected.scope_id)
    return 200, response, False


def mount_log_routes(application, helpers, resolve):
    @application.post('/log')
    async def log(request: Request):
        from .clients.native_prerequisite import PrerequisiteRefusal, strict_json
        try:
            if request.query_params:
                raise ValueError
            payload = LogRequest.model_validate(strict_json(await request.body(), limit=1024**2))
        except (ValueError, ValidationError, ApiProblem, PrerequisiteRefusal):
            raise _refuse() from None
        digest = await helpers.token_hash(request, request.headers.get('authorization'))
        key = helpers.required_idempotency_key(request.headers.get('idempotency-key'))
        alias = request.headers.get('x-cortex-scope')
        if not alias:
            raise _refuse(400, 'scope_required')
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await helpers.authenticate(request, connection, digest)
                context = await resolve(connection, principal, alias, [alias], write=True)
                status, data, replayed = await write_log(connection, context, payload, key,
                    request.headers.get('x-agent-name'))
        return helpers.replay_headers(JSONResponse(status_code=status, content=data), replayed)

    @application.get('/verify/write')
    async def verify(request: Request):
        if (set(request.query_params) != {'kind', 'id'} or len(request.query_params.multi_items()) != 2
                or request.query_params['kind'] not in KINDS):
            raise _refuse()
        _id(request.query_params['id'])
        digest = await helpers.token_hash(request, request.headers.get('authorization'))
        alias = request.headers.get('x-cortex-scope')
        if not alias:
            raise _refuse(400, 'scope_required')
        async with request.app.state.pool.acquire() as connection:
            async with connection.transaction():
                principal = await helpers.authenticate(request, connection, digest)
                context = await resolve(connection, principal, alias, [alias], write=False)
                data = await read_log(connection, context, request.query_params['kind'], request.query_params['id'])
        return JSONResponse(content=data)
