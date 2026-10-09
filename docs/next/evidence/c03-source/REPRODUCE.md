Run from the C03 SSD worktree. Dependencies are downloaded only, never installed on the host.

```text
mkdir -p tmp/wheels
python3 -m pip download --dest tmp/wheels --only-binary=:all: --platform manylinux2014_aarch64 --python-version 3.12 --implementation cp --abi cp312 --no-deps psycopg==3.2.9 psycopg-binary==3.2.9 typing-extensions==4.16.0
python3 docs/next/evidence/c03-source/run-c03.py green
```

The runner copies inputs and wheels into a disposable offline Podman pod, with separately bounded native-arm64 PG/Python containers and tmpfs. It records commands, exits, literal output, DB logs, wheel hashes and cleanup. Images must already exist at the recorded immutable IDs. PG 18.4 musl executes the database; Python 3.12 glibc/libpq executes the client. Test-only max/min WAL and explicit checkpoints bound repeated full-DDL mutation suites. This is no release image or platform qualification.
