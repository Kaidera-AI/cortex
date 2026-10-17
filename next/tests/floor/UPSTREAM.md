# Upstream and reuse

Cortex source pin: 7a1078213ac2497d6219e7778c7866001f97f30b; MIT, copyright 2026 Kaidera contributors.
Repository root LICENSE retains the full notice and disclaimer.
Python standard library: PSF licence; existing Python 3.13.7 and 3.14.0,
no vendoring or new package installation.

Reuse: unchanged original floor/crypto test files at the Cortex pin; hashes and
identities from accepted FLOOR-MAP.json, recorded in profile.json. Existing strict
manifest/classifier is the authority; no copied test bodies or new PG fixture.
JSON metadata only preserves admissions. Crypto provider and real PostgreSQL are
not substituted or newly installed.
LEAN: reused existing floor and oracle; no second release-admission engine, shrink
to 107 or archive/delete. Could merge duplicate profile reporting after accepted
parity; no retirement is granted here.
