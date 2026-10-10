# RM-001 forward-only retrieval authority repair

Nemo's exact-`5f0341c` review found that `auth-0002` grants existing tables
before `retrieval-0001` to `0003` create nine more. The C11b3 PG fixture
masked this with synthetic broad grants. Kai's C11b3 task requires the Core
installer to bind these schemas; acceptance stays open until the real request
role works on a fresh install and forged plain-GUC scope cannot read them.

1. RED: extend the installer ledger test to demand a new migration, least
   privileges for the request role, sequence usage, and direct forged-GUC
   denial. Commit the failing assertions before implementation.
2. Append `retrieval-0004` after the existing migrations. Keep Nemo's three
   SQL files and hashes byte-for-byte. Grant SELECT on all nine retrieval
   tables; grant INSERT/UPDATE/DELETE only on the disposable query cache and
   USAGE on its fence sequence. Add restrictive C04-bound policies for the
   request role so the existing plain-GUC policies cannot authorize it alone.
3. Remove fixture-only broad grants. Run the real C11 gateway, fresh installer
   ledger/RLS checks, a forged-context denial, source/SQL mutations, and
   affected regressions in bounded disposable PostgreSQL. Ask Nemo to
   re-review the additive migration at the new exact head.

Risk: an overbroad grant exposes cross-project retrieval rows; a missing grant
turns a valid read into 503. The migration is forward-only and no API startup
or live host migration is allowed.
