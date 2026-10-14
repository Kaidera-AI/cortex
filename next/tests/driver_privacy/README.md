# RC1 driver privacy oracle

Added controls; original c11_availability tests remain frozen. Fixture is exact primary psycopg3.2.9 connection_async.py from https://raw.githubusercontent.com/psycopg/psycopg/3.2.9/psycopg/psycopg/connection_async.py, SHA256 88a0216cb2adae48d8b739e35d29e3426dc049e70a05099c6d754c7d98dd79f7 (upstream copyright header retained; psycopg LGPL-3.0). Only connect/execute/wait/_try_cancel/close ASTs are projected unchanged. Explicit adapters replace libpq/cursor/socket wait/error inputs. This is offline source replay with synthetic canaries, no installed driver/native/backend/real credential exposure claim.

Root and direct driver handlers capture at DEBUG; scope covers every level, logger descendants, direct factory, cancellation and delayed close. A concurrent connection outside probe context must retain telemetry. Shared boundary also tested directly, nested, exception-safe and chaining an existing record factory. No AST log-call substitutions.

Run from next: PYTHONPATH=src:tests python3 -B tests/driver_privacy/run_offline.py <receipt.json>. This runner audits and forbids sockets/process creation.
