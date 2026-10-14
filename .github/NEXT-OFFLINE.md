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
The workflow supplies the independent PR base SHA and checks out its history.
The standalone `--base-ref` default is the accepted a47a152b checkpoint.
An unavailable or malformed base refuses; the manifest cannot supply its own
approval. Every accepted-base RUN entry that leaves RUN is listed in the
`transitions` receipt and fails unless its exclusion has checkable fixture proof.
The RC0 oracle at `next/docs/runtime/rc0/test_oracle.py` is covered by this rule
when it is added; that file is absent from the original PR155 base.

To add a suite, review its fixture effects and add the file to `run`. A new or
changed whole-file exclusion must include `{kind, source, sha256, symbol}`
evidence. The existing AST reader verifies the exact source digest, a PostgreSQL
connect call in the named fixture's setup, and that every removed method's class
requires that fixture through an unconditional, resolved setup/inheritance chain.
An unused import, a dead/unrelated setup call or a mixed plain method is not
proof. Unknown Python fixture composition refuses and needs review; this is a
conservative source proof, not an execution or qualification of PostgreSQL.
Unchanged accepted-base PG/native exclusions remain declared and visible.

Mixed files stay in RUN. `method_exclusions` names only exact unavailable nodes,
their reason and source-bound evidence. The two benchmark native nodes require
their explicit `B01_PODMAN=1` class gate; the one legacy-capture node requires
its two original input environment paths. All other methods in those three
files run (27 benchmark and five synthetic legacy methods). The worker collects
the whole file first, requires each declared unavailable node exactly once, then
removes only those nodes. Excluded methods receive no PASS credit; arbitrary
pytest deselection and skips still fail. Imported offline helpers remain usable.

Retirement record/original/v2 identities must agree with the independent base;
repinning edited reasons or bodies in the manifest does not approve them. Each
`retirement_checks` entry names the retained original deciding assertion, its
paired replacement assertion and a uniquely located, hash-bound product mutation.
The existing worker runs that replacement's healthy baseline and its mutant in
a disposable source copy. A kill requires a CALL AssertionError, passing setup
and teardown, exact named collection, no skip/xfail/deselection, observed product
execution and code fingerprints matching the actual source. Source bytes are
restored after every proof. All replacements also run normally in the full job.
This proves each declared deciding path, not arbitrary semantic equivalence;
reviewers must examine original-to-replacement meaning and any gate/base change.

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
