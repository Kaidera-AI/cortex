"""Materialize the migrated build catalog using the existing migrator connection."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import re

from .migrate import _assert_role_contract
from .native_prerequisite import NativeRefusal, payload_inventory

COMMAND_TIMEOUT = 5.0
RELATION_COLUMNS = {'name', 'rls', 'forced', 'app_direct_grant', 'app_owns'}


def _relations(rows) -> list[dict]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1024:
        raise NativeRefusal('cortex_health_degraded')
    values = []
    for row in rows:
        if (set(row.keys()) != RELATION_COLUMNS
                or not isinstance(row['name'], str)
                or re.fullmatch(r'cortex_(?:auth|core)\.[a-z_][a-z0-9_]{0,62}', row['name']) is None
                or any(type(row[key]) is not bool for key in RELATION_COLUMNS - {'name'})
                or row['app_owns'] is not False
                or row['rls'] and not row['forced']
                or not row['rls'] and row['app_direct_grant']):
            raise NativeRefusal('cortex_health_degraded')
        values.append({key: row[key] for key in ('name', 'rls', 'forced', 'app_direct_grant')})
    names = [row['name'] for row in values]
    if names != sorted(set(names)):
        raise NativeRefusal('cortex_health_degraded')
    return values


async def materialize_catalog(connection, *, source_revision, source_root=None,
                              migration_root=None) -> dict:
    """Observe one actual migrated snapshot, binding unchanged source payload bytes.

    The native builder independently binds source_revision to its frozen source and
    image identity. This unit neither opens a connection nor applies migrations.
    """
    if (not isinstance(source_revision, str)
            or re.fullmatch(r'[0-9a-f]{40}', source_revision) is None
            or type(COMMAND_TIMEOUT) not in (int, float)
            or not math.isfinite(COMMAND_TIMEOUT) or not 0 < COMMAND_TIMEOUT <= 5.0):
        raise NativeRefusal('cortex_bad_arguments')
    try:
        source = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
        migrations = Path(migration_root) if migration_root is not None else Path(__file__).resolve().parents[2] / 'migrations'
    except (TypeError, ValueError):
        raise NativeRefusal('cortex_bad_arguments') from None
    try:
        before = payload_inventory(source, migrations)
        expected = {name[len('migrations/'):]: digest for name, digest in before['files'].items()
                    if name.startswith('migrations/')}
        if (not any(name.startswith('src/') for name in before['files'])
                or not 1 <= len(expected) <= 1024
                or any(re.fullmatch(r'[0-9]{4}_[a-z0-9_]+\.sql', name) is None for name in expected)):
            raise NativeRefusal('cortex_image_mismatch')
        async with asyncio.timeout(COMMAND_TIMEOUT):
            async with connection.transaction(isolation='repeatable_read', readonly=True):
                role = await connection.fetchrow('''
                    SELECT current_user AS role_name, rolsuper, rolbypassrls
                      FROM pg_roles WHERE rolname=current_user
                ''')
                if (role is None or set(role.keys()) != {'role_name', 'rolsuper', 'rolbypassrls'}
                        or role['role_name'] != 'cortex_v2_migrator'
                        or role['rolsuper'] is not False or role['rolbypassrls'] is not False):
                    raise NativeRefusal('cortex_health_degraded')
                await _assert_role_contract(connection)
                rows = await connection.fetch('''
                    SELECT n.nspname || '.' || c.relname AS name,
                           c.relrowsecurity AS rls, c.relforcerowsecurity AS forced,
                           pg_get_userbyid(c.relowner)='cortex_v2_app' AS app_owns,
                           (has_table_privilege('cortex_v2_app',c.oid,'SELECT')
                            OR has_table_privilege('cortex_v2_app',c.oid,'INSERT')
                            OR has_table_privilege('cortex_v2_app',c.oid,'UPDATE')
                            OR has_table_privilege('cortex_v2_app',c.oid,'DELETE')) AS app_direct_grant
                      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                     WHERE n.nspname IN ('cortex_auth','cortex_core') AND c.relkind IN ('r','p')
                     ORDER BY name LIMIT 1025
                ''')
                relations = _relations(rows)
                ledger_rows = await connection.fetch('''
                    SELECT migration_id, checksum_sha256 FROM cortex_core.schema_migrations
                     ORDER BY migration_id LIMIT 1025
                ''')
                if not isinstance(ledger_rows, list) or len(ledger_rows) != len(expected):
                    raise NativeRefusal('cortex_health_degraded')
                ledger = {}
                for row in ledger_rows:
                    if (set(row.keys()) != {'migration_id', 'checksum_sha256'}
                            or not isinstance(row['migration_id'], str)
                            or row['migration_id'] in ledger
                            or row['migration_id'] not in expected
                            or row['checksum_sha256'] != expected[row['migration_id']]):
                        raise NativeRefusal('cortex_health_degraded')
                    ledger[row['migration_id']] = row['checksum_sha256']
                if ledger != expected:
                    raise NativeRefusal('cortex_health_degraded')
                if payload_inventory(source, migrations) != before:
                    raise NativeRefusal('cortex_image_mismatch')
                value = {'schema': 'cortex.rls-inventory.v1', 'source_revision': source_revision,
                         'api_source_payload_sha256': before['sha256'],
                         'migrations': expected, 'relations': relations}
                if len(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()) + 1 > 65536:
                    raise NativeRefusal('cortex_image_mismatch')
        return value
    except NativeRefusal:
        raise
    except Exception:
        raise NativeRefusal('cortex_health_degraded') from None
