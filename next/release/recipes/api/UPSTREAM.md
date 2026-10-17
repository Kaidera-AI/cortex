# UPSTREAM (recipes/api inputs)
Every wheel below is pinned by hash in `requirements.linux-{amd64,arm64}.lock`, recorded with size, licence and requirements in `inputs-receipt.json`, and lives in the ONE archive
`/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/nix-a-day1-assets-20261017-r2` (the 0871c9c5 snapshot's archive `nix-a-day1-assets-20261017` stays as it was). Obligations are recorded, none is a hard blocker (H-D594).
| distribution | source | licence | obligation / change |
|---|---|---|---|
| anyio 4.12.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT | keep notice |
| asyncpg 0.32.0 (added 17 Oct, J01) | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | Apache-2.0 | keep LICENSE (+ NOTICE if present) in dist-info |
| attrs 25.4.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT | keep notice |
| certifi 2025.11.12 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MPL-2.0 | unmodified, keep notice, source offer for the file. CHECK for the CTO |
| cffi 2.1.1 (added 17 Oct, J01) | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT-0 | none |
| click 8.5.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | BSD-3-Clause | keep notice + disclaimer |
| cryptography 50.0.0 (added 17 Oct, J01) | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | Apache-2.0 OR BSD-3-Clause | keep both licence files in dist-info |
| h11 0.16.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT | keep notice |
| httpcore 1.0.9 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | BSD-3-Clause | keep notice + disclaimer |
| httpx 0.28.1 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | BSD-3-Clause | keep notice + disclaimer |
| idna 3.11 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | BSD-3-Clause | keep notice + disclaimer |
| jsonschema 4.25.1 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT | keep notice |
| jsonschema-specifications 2025.9.1 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT | keep notice |
| psycopg 3.2.13 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | LGPL-3.0-only | unmodified, replaceable (venv, not vendored), licence text, source offer. CHECK for the CTO |
| psycopg-binary 3.2.13 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | LGPL-3.0-only | unmodified, replaceable (venv, not vendored), licence text, source offer. CHECK for the CTO |
| pycparser 3.0 (added 17 Oct, J01) | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | BSD-3-Clause | keep notice + disclaimer |
| referencing 0.37.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT | keep notice |
| rpds-py 0.30.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT | keep notice |
| sniffio 1.3.1 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | MIT OR Apache-2.0 | keep notices |
| starlette 0.50.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | BSD-3-Clause | keep notice + disclaimer |
| typing-extensions 4.16.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | PSF-2.0 | keep notice |
| uvicorn 0.40.0 | PyPI, sha256 per arch in requirements.linux-<arch>.lock and inputs-receipt.json | BSD-3-Clause | keep notice + disclaimer |

- asyncpg: the runtime calls `_require_asyncpg()` (runtime/pg_adapters.py, auth.py:144); no dependencies on Python 3.14.
- cryptography 50.0.0 + cffi + pycparser: the signature-verification closure only (runtime/member_admission -> release/trust._verify_original -> release/ed25519.verify_ed25519, which imports cryptography lazily and refuses any version except 50.0.0). Added by cox's ruling of 17 Oct; it does not ratify encryption, token custody or a provider. cryptography musllinux wheels bundle OpenSSL (Apache-2.0).
- recipe, apk pins and the other wheel locks: carried unchanged from branch `nix/ga2-recipes-20261016` (content of 514299a9), review input, not an acceptance of ga2.
- Tools: `tools/inputs.py` (receipt, verify, smoke) is ours, stdlib only.
