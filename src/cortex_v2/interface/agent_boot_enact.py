"""Typed boot appends and their receipts commit atomically, never as grants."""
from __future__ import annotations

import uuid

import asyncpg
from pydantic import ValidationError

from ..agent_boot_models import BootAgentBindRequest, BootEntryBindRequest, BootPublicationRequest
from ..receipts import begin_command, commit_receipt, request_digest
from ..store import ApiProblem

STREAMS = {
    'agent': ('boot_agent_binding_revisions', 'boot.agent.bind', BootAgentBindRequest, 'actor_id'),
    'entry': ('boot_entry_binding_revisions', 'boot.entry.bind', BootEntryBindRequest, 'binding_id'),
    'publication': ('boot_catalogue_publication_revisions', 'boot.catalogue.publish', BootPublicationRequest, 'publication_id'),
}


async def _enact(connection, context, idempotency_key, payload, stream):
    table, operation, model, key_field = STREAMS[stream]
    try:
        payload = model.model_validate(payload.model_dump() if isinstance(payload, model) else payload)
    except ValidationError:
        raise ApiProblem(422, 'invalid_request', 'A valid typed boot enactment is required.') from None
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise ApiProblem(400, 'idempotency_key_required', 'Provide an Idempotency-Key of at most 128 characters.')
    scope, principal = context.selected, context.principal
    if scope.kind != 'project' or not scope.can_write or context.read_scopes != (scope,):
        raise ApiProblem(403, 'boot_enact_denied', 'Current boot enactment authority is required.')
    # One transaction/savepoint contains authority, optimistic-head append and
    # receipt. A receipt or driver failure cannot leave a committed bare append.
    try:
        async with connection.transaction():
            authority = ('cortex_context.boot_current_owner($3,$1)' if stream == 'publication'
                         else 'cortex_context.boot_project_manager($2,$1)')
            allowed = await connection.fetchval(f'''SELECT
                NULLIF(current_setting('cortex.principal_id',true),'')::uuid=$1
                AND cortex_core.caller_installation_matches($3)
                AND cortex_core.scope_read_policy($2)
                AND cortex_core.scope_write_policy($2)
                AND {authority}''', principal.principal_id,scope.scope_id,principal.installation_id)
            if not allowed:
                raise ApiProblem(403, 'boot_enact_denied', 'Current boot enactment authority is required.')
            receipt_binding = ({'installation_id':principal.installation_id} if stream == 'publication'
                               else {'scope_id':scope.scope_id})
            digest = request_digest({'operation':operation,'project_scope_id':str(scope.scope_id),
                'installation_id':str(principal.installation_id),'request':payload.model_dump(mode='json')})
            previous, replayed = await begin_command(connection,principal_id=principal.principal_id,
                operation=operation,idempotency_key=idempotency_key,digest=digest,**receipt_binding)
            if replayed and previous is not None:
                return 200, previous, True
            stream_id = getattr(payload,key_field) or uuid.uuid4()
            stream_scope = principal.installation_id if stream == 'publication' else scope.scope_id
            scope_field = 'installation_id' if stream == 'publication' else 'project_scope_id'
            # This is exactly the migration trigger's stream lock, held before
            # reading MAX. Distinct idempotency keys cannot race past a head.
            await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                                     table+':'+str(stream_scope)+':'+str(stream_id))
            revision = await connection.fetchval(f'''SELECT COALESCE(max(revision),0)
                FROM cortex_context.{table} WHERE {scope_field}=$1 AND {key_field}=$2''',stream_scope,stream_id)
            if revision != payload.expected_revision:
                raise ApiProblem(409, 'boot_revision_conflict', 'The expected boot stream head is stale.')
            data = payload.model_dump()
            data.pop('expected_revision')
            data[key_field] = stream_id
            data[scope_field] = stream_scope
            data['revision'] = revision+1
            data['published_by_principal' if stream == 'publication' else 'enacted_by_principal'] = principal.principal_id
            columns = ','.join(data)
            values = ','.join('$'+str(index) for index in range(1,len(data)+1))
            await connection.execute(f'INSERT INTO cortex_context.{table}({columns}) VALUES({values})',*data.values())
            receipt = {'schema_version':'cortex.boot-enactment-receipt.v1','state':'committed',
                'operation':operation,'stream_id':str(stream_id),'revision':revision+1,
                'project_scope_id':str(scope.scope_id),'installation_id':str(principal.installation_id),
                'enacted_by_principal':str(principal.principal_id),'source_reference':payload.source_reference}
            await commit_receipt(connection,principal_id=principal.principal_id,operation=operation,
                idempotency_key=idempotency_key,digest=digest,receipt_kind='committed',receipt=receipt,**receipt_binding)
            return 201, receipt, False
    except asyncpg.InsufficientPrivilegeError:
        raise ApiProblem(403, 'boot_enact_denied', 'Current boot enactment authority is required.') from None
    except asyncpg.UniqueViolationError:
        raise ApiProblem(409, 'boot_revision_conflict', 'The boot stream or receipt identity already exists.') from None
    except (asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError):
        raise ApiProblem(422, 'boot_fact_mismatch', 'The binding does not match current registered canonical facts.') from None


async def enact_boot_agent(connection, context, idempotency_key, payload, path_params):
    return await _enact(connection,context,idempotency_key,payload,'agent')


async def enact_boot_entry(connection, context, idempotency_key, payload, path_params):
    return await _enact(connection,context,idempotency_key,payload,'entry')


async def publish_boot_catalogue(connection, context, idempotency_key, payload, path_params):
    return await _enact(connection,context,idempotency_key,payload,'publication')
