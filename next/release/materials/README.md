# R02 offline material preparation

These are upstream dependency materials for R1. They do not supply Cortex's
runtime, final source F, R01 input admission, image inspector authority, native
receipt emission or the R2 final product recipes. The existing guards stay closed.

`origins.json` retains the ratified versions, exact selected wheel identities and
base manifest digests. The two requirements files are formatted projections of
that same production wheel closure, for normal GIL CPython 3.14.8 on Debian 13.
Verifier/installer-only dependencies and optional runtime extras are excluded.
The pgvector archive uses the independently pinned Git commit; its SHA256 is a
measurement of that original archive, not an upstream-published checksum.

Acquire fresh wheel/source originals into a new directory:

```sh
python3 next/release/materials/acquire.py --output /explicit/new/material-directory
```

This uses TLS certificate verification and refuses changed or missing pins.
On a Python installation without a configured CA bundle, set standard
`SSL_CERT_FILE` to an existing trusted system bundle. It never disables TLS.
Retain `ACQUIRED.json` and the original PyPI metadata beside the payloads.
Acquisition is not independent license or release acceptance; Bob owns licenses.

For an authorized native amd64 preparation run, use a dedicated rootless engine
store. Pull the two amd64 manifest digests from `origins.json` into that store.
Acquire the build-essential dependency closure and the server-dev package whose
version exactly matches the pinned PostgreSQL server. Retain every `.deb`,
SHA256, signed APT metadata and original package/control/license inventory. All
package acquisition is separate from the offline build.

Prepare two bounded contexts, with `.dockerignore` copied beside each:

- Core: `debs/` contains the complete downloaded compiler/header closure;
  `pgvector/` contains the pinned archive's extracted source.
- API: `wheels/` contains the 17 amd64 originals; `requirements.txt` is the amd64
  lock; `libpq/` contains libpq18.6 and every resolved DT_NEEDED library from the
  pinned PostgreSQL image, plus the measured inventory and original licenses.

Build with explicit immutable `CORE_BASE`/`API_BASE` arguments, a recorded
`SOURCE_DATE_EPOCH`, `--pull=never --network=none --no-cache --layers=false`, and
`--source-date-epoch`/`--rewrite-timestamp`. Use `core.Containerfile` or
`api.Containerfile` explicitly. The Core recipe installs only supplied local
archives; APT's `--no-download` is omitted because it also prevents APT's local
file-copy method. The engine's network namespace enforces the offline boundary.
The compile stage preserves upstream pgvector optimization flags while disabling
`-march=native`; compiler/header packages remain outside the final material image.
The API stage registers the copied library directory with ldconfig so pure
Python psycopg can discover libpq without adding a compiler or objdump. It removes
ldconfig's auxiliary cache, and uses pip's existing hash checking and offline resolver, with no
source distributions, a new resolver or package cache. The pinned image interpreter
then compiles every dependency source at optimization 0 with standard compileall,
checked-hash invalidation and PYTHONHASHSEED=0. The compile step strips the staging
prefix and prepends /opt/cortex/python, so code objects name their installed material
paths. The final stage copies these caches with the exact sources.

Core has a separately exportable `--target sdk` stage containing that same local
compiler/header installation; the normal final target still excludes the toolchain.
Retain the SDK image, package originals and measured gcc/cc1/linker/header/library
identities as R1 inputs. Check the compiler against the actual upstream pgvector
source. This SDK is an input, not a shipped Core image or a native qualification.

For API cache evidence, use the existing R02 build.audit.audit_all_caches under
the same CPython 3.14.8 image. Inventory every .py and .pyc, require exact source
coverage, validate magic/flags/source-hash/recursive code body/final filename and
reject changed flags, source, body, filename, trailing data and empty inventory.
These caches cover dependency preparation only. Final Cortex source caches and
any final owner path change require regeneration and independent R01 admission.

The output images are dependency preparation images. Core retains upstream
PostgreSQL's entrypoint for the extension smoke; API retains the upstream Python
entrypoint and installs no Cortex launcher. Test them offline as non-root users:
create/query pgvector0.8.7 on PostgreSQL18.6, import all17 pinned distributions,
check pure Python psycopg loads libpq18.6, execute psycopg and asyncpg queries,
run pip check and verify only the loopback interface is present. Record actual
ELF identities and installed-file inventories. Missing/corrupt payload controls
must fail. Export new OCI material archives and generate fresh SBOMs; these
outputs are not the four Cortex product archives or R6 native A/B receipts.

ARM wheel payloads can be acquired and checked without executing them. ARM
compilation/execution and the test MacBook wait for Kai's explicit host GO.
Final runtime/library configuration, bytecode paths, service accounts, shutdown
contract, source contexts and four build-input originals join Cox's actual F in
R2. Do not mark source envelopes BOUND from these preparation results.
