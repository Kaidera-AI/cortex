"""Real PostgreSQL portability and unchanged final-authority boundaries."""
from pathlib import Path
import sys
from uuid import UUID

from psycopg import sql
from cortex_core.auth import AuthError
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'auth_identity'))
import test_identity as frozen
from core_db_fixture import Fixture, uid


class IdentityPortability(Fixture):
    port = frozen.IdentityTests.port
    register = frozen.IdentityTests.register
    counts = frozen.IdentityTests.counts

    def test_python_role_order_is_valid_under_real_non_c_collation(self):
        roles = ('r-', 'r.', 'r_')
        self.assertEqual(frozen.identity_api._roles(roles), roles)
        row = self.admin.execute("SELECT collname FROM pg_collation WHERE collprovider='i' AND collencoding IN(-1,6) ORDER BY (collname='und-x-icu') DESC,collname LIMIT 1").fetchone()
        self.assertIsNotNone(row, 'copied PostgreSQL must supply a real ICU collation for portability proof')
        collation = sql.Identifier(row[0])
        actual = self.admin.execute(sql.SQL('SELECT array_agg(r ORDER BY r COLLATE {}) FROM unnest(%s::text[]) r').format(collation), (list(roles),)).fetchone()[0]
        self.assertNotEqual(actual, list(roles), 'real non-C comparison must falsify the byte-order assumption')
        valid = self.admin.execute(sql.SQL('SELECT auth.identity_roles_valid(%s::text[] COLLATE {})').format(collation), (list(roles),)).fetchone()[0]
        self.assertTrue(valid, 'SQL validation must use the same byte order as the private Python port')

    def test_current_authorizing_key_rotation_rolls_back_and_returns_no_secret(self):
        actor = self.register(permissions=('owner',))
        port = self.port(actor.secret)
        before = self.counts()
        with self.assertRaises(AuthError):
            port.rotate(actor.principal_id, actor.credential_id, request_key='self-rotate')
        self.assertEqual(self.counts(), before)
        self.assertIsNone(self.admin.execute('SELECT revoked_at FROM auth.credentials WHERE id=%s', (actor.credential_id,)).fetchone()[0])
        self.assertEqual(port.lookup(actor.principal_id).principal_id, UUID(uid(70)))

    def test_current_authorizing_key_revocation_rolls_back_and_remains_valid(self):
        actor = self.register(permissions=('owner',))
        port = self.port(actor.secret)
        before = self.counts()
        with self.assertRaises(AuthError):
            port.revoke(actor.credential_id, request_key='self-revoke')
        self.assertEqual(self.counts(), before)
        self.assertIsNone(self.admin.execute('SELECT revoked_at FROM auth.credentials WHERE id=%s', (actor.credential_id,)).fetchone()[0])
        self.assertEqual(port.lookup(actor.principal_id).principal_id, UUID(uid(70)))
