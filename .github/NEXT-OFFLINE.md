# Hosted offline v2 source contracts

`next-offline.yml` runs Python 3.14.7 with the unchanged API dependency hash lock
and the additional test-only hash lock. All actions use full commit pins, the
token has only `contents: read`, and checkout does not persist credentials.
The suite service uses a separate network namespace, denies IPv4/IPv6 socket
creation, and verifies those kernel restrictions before running any tests.
It has a 1 GiB memory ceiling, no swap, two CPUs, and no new privileges.
Dependency installation occurs before the offline service starts.

`.github/next-offline-suites.json` partitions every `test_*.py` and `*_test.py`
file recursively under `next/`. Entries are individual paths, not patterns.
An unlisted file, a stale or duplicated entry, a symlink, an empty RUN list,
or an exclusion without a one-line reason fails before test execution.
The RC0 oracle at `next/docs/runtime/rc0/test_oracle.py` is covered by this rule
when it is added; that file is absent from the original PR155 base.

To add a suite, review its fixture effects and add the whole file to `run`, or
add `{ "path": "next/.../test_name.py", "reason": "Needs ..." }` to `excluded`.
Database, container, native signing/encryption, and external captured-artifact
fixtures are excluded explicitly. Mixed files with gated native classes are
excluded whole; the job does not remove classes, select with `-k`, or accept
skips. Offline fixture consumers may import helper functions from excluded
files without running those files' native fixtures.

Each RUN file has a fresh pytest interpreter, disabled plugin autoload, no
repository pytest addopts, and an owned temporary case root. Bare imports used
by the historical tests have explicit source/test search paths. Child tests
receive no inherited credentials or database endpoint variables. The job uses
normal pytest discovery of dynamically generated methods and honors a module's
standard unittest `load_tests` protocol through a dedicated pytest collector.
The receipt fixture's nine outer classifier controls therefore run as declared;
its intentionally failing probe classes run inside those controls. Every declared
TestCase outcome is recorded, including inner errors, skips and expected failures.
The two standalone adoption scripts receive the current source and an owned
temporary output root as positional arguments. Their test bodies are unchanged.
Separately invoked
native factories such as B01's `native_suite()` are **NOT_RUN**.

The unchanged PR155 validator checks a nonempty unique collection and exactly
one passing setup/call/teardown report per selected item. Skips, xfails, xpasses,
deselections, collection/setup/body/teardown errors, and nonzero raw pytest exits
fail. The parent additionally checks the actual child exit, the matching suite
and source hash, and that no selected node comes from another file. No per-file
test count is hardcoded: adding test methods to an admitted file runs them through
normal discovery. The summary and per-suite JSON/logs are uploaded for seven days.

The inventory/outcome oracle can run without test dependencies:

```sh
python -B .github/scripts/tests/test_next_offline_gate.py
python -B .github/scripts/pytest_next_offline.py --inventory-only --output /tmp/next-inventory
```

`--force-skip` is a deliberately failing verification control, never a bypass.
Hosted control commits also add an actual unlisted suite and remove an actual
manifest entry. The final branch restores all those transient changes.

This job proves offline source contracts. It does not prove PostgreSQL, native
images, installed-host acceptance, or G_OLD. A PR editing the gate, manifest or
workflow itself can evade enforcement; independent review and repository
protections must adjudicate such changes. An unimplemented source contract
remains RED; it is not relabeled as native merely to obtain a green check.
