"""Finite commands inside the owned API image; no public listener.

Database credentials stay in this container's existing app configuration. Recipient
tokens leave only in the initial private create response to the host KeyStore port.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import select
import time
from pathlib import Path
import re
import sys

from .clients.native_prerequisite import PrerequisiteRefusal, canonical_uuid, strict_json


COMMAND_TIMEOUT = 5.0


class NativeRefusal(PrerequisiteRefusal):
    pass


def payload_inventory(source: Path, migrations: Path) -> dict:
    try:
        files = {}; total = 0
        for label, root in (('src', source), ('migrations', migrations)):
            if root.is_symlink() or not root.is_dir(): raise ValueError
            for path in sorted(root.rglob('*')):
                if path.is_symlink(): raise ValueError
                if not path.is_file(): continue
                if path.suffix == '.pyc' and '__pycache__' in path.parts: continue
                if path.stat().st_size > 8 * 1024 * 1024: raise ValueError
                raw = path.read_bytes(); total += len(raw)
                if total > 64 * 1024 * 1024: raise ValueError
                files[label + '/' + path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'files': files, 'sha256': digest}
    except (OSError, ValueError, UnicodeError):
        raise NativeRefusal('cortex_image_mismatch') from None


async def read_native_database(connection, principal, *, project: str, project_root: str,
                               migrations: dict, expected_relations: list) -> dict:
    """SELECT-only catalog and selected project observations inside a read-only tx."""
    role = await connection.fetchrow('''
        SELECT current_user AS role_name, rolsuper, rolbypassrls,
               pg_has_role(current_user, 'cortex_v2_migrator', 'MEMBER') AS migrator_member
          FROM pg_roles WHERE rolname=current_user
    ''')
    if (not role or role['role_name'] != 'cortex_v2_app' or role['rolsuper'] is not False
            or role['rolbypassrls'] is not False or role['migrator_member'] is not False):
        raise NativeRefusal('cortex_health_degraded')
    rows = await connection.fetch('''
        SELECT n.nspname || '.' || c.relname AS name,
               c.relrowsecurity AS rls, c.relforcerowsecurity AS forced,
               pg_get_userbyid(c.relowner)=current_user AS app_owns,
               (has_table_privilege(current_user,c.oid,'SELECT')
                OR has_table_privilege(current_user,c.oid,'INSERT')
                OR has_table_privilege(current_user,c.oid,'UPDATE')
                OR has_table_privilege(current_user,c.oid,'DELETE')) AS app_direct_grant
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname IN ('cortex_auth','cortex_core') AND c.relkind IN ('r','p')
         ORDER BY name
    ''')
    columns = ('name', 'rls', 'forced', 'app_direct_grant')
    actual = {row['name']: {key: row[key] for key in columns} for row in rows}
    expected = {row['name']: {key: row[key] for key in columns} for row in expected_relations}
    if (not rows or len(actual) != len(rows) or len(expected) != len(expected_relations)
            or actual != expected or any(row['app_owns'] is not False for row in rows)
            or any((row['rls'] is True and row['forced'] is not True)
                   or (row['rls'] is not True and row['app_direct_grant'] is not False) for row in rows)):
        raise NativeRefusal('cortex_health_degraded')
    ledger_rows = await connection.fetch('SELECT migration_id, checksum_sha256 FROM cortex_core.schema_migrations')
    ledger = {row['migration_id']: row['checksum_sha256'] for row in ledger_rows}
    if not migrations or ledger != migrations or len(ledger) != len(ledger_rows):
        raise NativeRefusal('cortex_health_degraded')
    binding = await connection.fetchrow('''
        SELECT p.project_scope_id::text AS scope_id, a.alias AS primary_alias,
               p.repo_root AS primary_root
          FROM cortex_core.project_registry p
          JOIN cortex_core.scope_aliases a ON a.scope_id=p.project_scope_id
         WHERE a.alias=$1 AND a.is_primary AND a.retired_at IS NULL AND p.status='active'
    ''', project)
    if not binding or binding['primary_alias'] != project or binding['primary_root'] != project_root:
        raise NativeRefusal('cortex_instance_mismatch')
    canonical_uuid(binding['scope_id'])
    return {'migration_checksums_match': True, 'required_rls_enabled_and_forced': True,
            'app_role_non_superuser_without_bypassrls': True,
            'app_role_not_migrator_or_table_owner': True, 'database_instance_matches': True,
            'project_binding': dict(binding)}


def _frame(value):
    from .models import CreateProjectRequest
    try:
        if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False).encode()) > 65536:
            raise ValueError
        mode = value['mode']
        if mode == 'payload':
            if set(value) != {'mode'}: raise ValueError
            return mode, None
        canonical_uuid(value['installation_id'])
        if not isinstance(value['credential'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value['credential']):
            raise ValueError
        if mode == 'authorize-owner':
            if set(value) != {'mode', 'installation_id', 'credential'}:
                raise ValueError
            return mode, None
        if mode == 'create-project':
            if (set(value) != {'mode', 'installation_id', 'credential', 'idempotency_key', 'request'}
                    or not isinstance(value['idempotency_key'], str)
                    or not re.fullmatch(r'[\x21-\x7e]{1,128}', value['idempotency_key'])):
                raise ValueError
            body = CreateProjectRequest.model_validate(value['request'])
            if body.source_project is not None or body.with_console is not True:
                raise ValueError
            return mode, body
        if mode == 'readiness':
            if (set(value) != {'mode', 'installation_id', 'credential', 'project', 'project_root', 'migrations', 'expected_relations'}
                    or not isinstance(value['project'], str) or not 1 <= len(value['project']) <= 128
                    or not isinstance(value['project_root'], str) or not value['project_root'].startswith('/')
                    or not isinstance(value['migrations'], dict) or not value['migrations']
                    or not isinstance(value['expected_relations'], list) or not value['expected_relations']):
                raise ValueError
            return mode, None
        raise ValueError
    except (KeyError, TypeError, ValueError, PrerequisiteRefusal):
        raise NativeRefusal('cortex_provisioning_setup_required') from None


async def _select_readiness_scope(connection, principal, project):
    from . import store
    context = await store.resolve_scopes(connection, principal, project, [project], write=False)
    if context.selected.kind != 'project' or context.selected.can_read is not True:
        raise NativeRefusal('cortex_credential_refused')
    return context


async def execute_private(value: dict, *, settings=None, connect=None) -> dict:
    mode, body = _frame(value)
    if mode == 'payload':
        return payload_inventory(Path(__file__).resolve().parents[1], Path(__file__).resolve().parents[2] / 'migrations')
    from . import identity, store
    from .config import Settings
    import asyncpg
    settings = Settings.from_env() if settings is None else settings
    connect = asyncpg.connect if connect is None else connect
    connection = None
    deadline = asyncio.get_running_loop().time() + COMMAND_TIMEOUT
    try:
        async with asyncio.timeout_at(deadline):
            connection = await connect(settings.database_url, timeout=min(5, COMMAND_TIMEOUT), command_timeout=min(5, COMMAND_TIMEOUT))
            async with connection.transaction(readonly=mode != 'create-project'):
                principal = await store.authenticate(connection, store.token_digest(value['credential'], settings.token_pepper))
                if str(principal.installation_id) != value['installation_id']:
                    raise NativeRefusal('cortex_instance_mismatch')
                if mode == 'authorize-owner':
                    try:
                        await identity.list_privileged_actions(connection, principal, 1)
                    except store.ApiProblem:
                        raise NativeRefusal('cortex_provisioning_owner_required') from None
                    except Exception:
                        raise NativeRefusal('cortex_health_unavailable') from None
                    return {'authorized': True, 'installation_id': value['installation_id']}
                if mode == 'create-project':
                    try:
                        # Existing DB-mediated audit read is owner-only. Discard its body.
                        await identity.list_privileged_actions(connection, principal, 1)
                    except store.ApiProblem:
                        raise NativeRefusal('cortex_provisioning_owner_required') from None
                    _, result, _ = await identity.create_project(connection, principal, settings.token_pepper,
                                                                   body, value['idempotency_key'])
                    return result
                scope = await _select_readiness_scope(connection, principal, value['project'])
                result = await read_native_database(connection, principal, project=value['project'],
                           project_root=value['project_root'], migrations=value['migrations'],
                           expected_relations=value['expected_relations'])
                if result['project_binding']['scope_id'] != str(scope.selected.scope_id):
                    raise NativeRefusal('cortex_instance_mismatch')
                return result
    except NativeRefusal:
        raise
    except store.ApiProblem as error:
        code = ('cortex_credential_refused' if error.status in (401, 403)
                else 'cortex_provisioning_conflict' if error.status in (409, 422)
                else 'cortex_health_unavailable')
        raise NativeRefusal(code, http_status=error.status) from None
    except asyncio.CancelledError:
        raise NativeRefusal('cortex_provisioning_reissue_required' if mode == 'create-project'
                            else 'cortex_health_unavailable') from None
    except (OSError, asyncpg.PostgresError, TimeoutError, ValueError):
        raise NativeRefusal('cortex_provisioning_reissue_required' if mode == 'create-project'
                            else 'cortex_health_unavailable') from None
    finally:
        if connection is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if asyncio.current_task().cancelling() or remaining <= 0:
                connection.terminate()
            else:
                try:
                    await asyncio.wait_for(connection.close(), timeout=remaining)
                except (TimeoutError, asyncio.CancelledError, OSError, asyncpg.PostgresError):
                    connection.terminate()
                    raise NativeRefusal('cortex_provisioning_reissue_required' if mode == 'create-project'
                                        else 'cortex_health_unavailable') from None


def _private_input(deadline: float) -> bytes:
    """Wait for EOF within the command budget, even when the peer retains its pipe."""
    raw = bytearray()
    fd = sys.stdin.fileno()
    while len(raw) <= 65536:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise NativeRefusal('cortex_health_unavailable')
        part = os.read(fd, 65537 - len(raw))
        if not part:
            break
        raw.extend(part)
    return bytes(raw)


def main() -> int:
    try:
        deadline = time.monotonic() + COMMAND_TIMEOUT
        value = strict_json(_private_input(deadline))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeRefusal('cortex_health_unavailable')
        result = asyncio.run(asyncio.wait_for(execute_private(value), timeout=remaining))
        output = json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        if len(output) > 65536: raise NativeRefusal('cortex_health_unavailable')
        sys.stdout.buffer.write(output); sys.stdout.buffer.flush()
        return 0
    except Exception as error:
        refusal = error if isinstance(error, PrerequisiteRefusal) else NativeRefusal('cortex_health_unavailable')
        sys.stderr.write(json.dumps(refusal.public(), sort_keys=True) + '\n')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
