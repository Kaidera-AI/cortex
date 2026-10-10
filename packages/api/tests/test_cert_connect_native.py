"""Opt-in real asyncpg certificate/password-hba proof in an owned no-network pod.

The runner supplies two explicit disposable PostgreSQL endpoints, generated PKI,
and a memory-only password. No default/runtime DSN or mocked driver is used.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import unittest
from urllib.parse import urlsplit

import asyncpg
import db_tls


@unittest.skipUnless(os.environ.get('CORTEX_REQUIRE_NATIVE_CERT_TESTS') == '1',
                     'requires explicitly owned certificate/password PostgreSQL fixture')
class CertificateConnectNative(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertEqual(asyncpg.__version__, '0.31.0')
        self.assertEqual(os.environ.get('CORTEX_DB_AUTH'), 'certificate')
        self.assertTrue(Path('/run/.containerenv').exists())
        self.assertTrue(os.environ.get('CORTEX_CERT_FIXTURE_LIFECYCLE', '').startswith('nemo-cert-'))
        self.cert = os.environ['CORTEX_CERT_TEST_DSN']
        self.password = os.environ['CORTEX_PASSWORD_TEST_DSN']
        for dsn, port in [(self.cert, 5432), (self.password, 5433)]:
            parsed = urlsplit(dsn)
            self.assertEqual((parsed.scheme, parsed.hostname, parsed.port, parsed.username,
                              parsed.password, parsed.path, parsed.query, parsed.fragment),
                             ('postgresql', 'cortex-pg', port, 'postgres', None,
                              '/platform_agent_memory', '', ''))

    async def test_certificate_connection_uses_exact_kwargs(self):
        try:
            conn = await asyncpg.connect(self.cert, **db_tls.connection_kwargs(self.cert, 'admin'))
        except Exception as exc:
            self.fail(f'certificate connection failed in real asyncpg: {type(exc).__name__}: {exc}')
        try:
            self.assertEqual(await conn.fetchval('SELECT current_user'), 'postgres')
            self.assertTrue(await conn.fetchval('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()'))
            methods = await conn.fetch("SELECT auth_method FROM pg_hba_file_rules WHERE type='hostssl'")
            self.assertTrue(methods)
            self.assertEqual({row['auth_method'] for row in methods}, {'cert'})
        finally:
            await conn.close()

    async def test_exact_compose_migrator_applies_schema(self):
        env = dict(os.environ, PYTHONPATH='/app', CORTEX_PG_DSN_ADMIN=self.cert)
        command = ['python', '/app/standalone/migrate.py']
        result = await asyncio.to_thread(subprocess.run, command, env=env, cwd='/app',
                                         capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout.splitlines()[-1])
        self.assertGreater(receipt['baseline_adopted'], 0)
        self.assertGreater(receipt['applied'], 0)
        self.assertEqual(receipt['historical_replayed'], 0)
        self.assertTrue(receipt['tail_actions'])
        second = await asyncio.to_thread(subprocess.run, command, env=env, cwd='/app',
                                         capture_output=True, text=True, timeout=90)
        self.assertEqual(second.returncode, 0, second.stderr)
        repeated = json.loads(second.stdout.splitlines()[-1])
        self.assertEqual(repeated['baseline_adopted'], 0)
        self.assertEqual(repeated['applied'], 0)
        self.assertEqual(repeated['historical_replayed'], 0)

    async def test_pgpassword_is_not_used_on_password_hba(self):
        secret = os.environ['PGPASSWORD']
        self.assertTrue(secret)
        options = db_tls.connection_kwargs(self.password, 'admin')
        self.assertEqual(options['passfile'], os.devnull)
        # Positive control proves the different real server accepts THIS password.
        control = await asyncpg.connect(self.password, ssl=options['ssl'],
                                        password=secret, passfile=os.devnull)
        try:
            self.assertTrue(await control.fetchval('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()'))
            methods = await control.fetch("SELECT auth_method FROM pg_hba_file_rules WHERE type IN ('host','hostssl')")
            self.assertTrue(methods)
            self.assertEqual({row['auth_method'] for row in methods}, {'scram-sha-256'})
        finally:
            await control.close()
        try:
            conn = await asyncpg.connect(self.password, **options)
        except asyncpg.InvalidPasswordError:
            return  # The server refused the explicitly empty password.
        except Exception as exc:
            self.fail(f'expected server password refusal, got {type(exc).__name__}')
        else:
            await conn.close()
            self.fail('ambient PGPASSWORD unexpectedly authenticated the certificate configuration')
