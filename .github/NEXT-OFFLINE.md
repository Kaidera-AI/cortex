# Hosted offline v2 source contracts

## Bounded execution of the U3 bridge

The live manifest keeps `test_original_member_bridge.py` in RUN and declares
`partitions: {"next/tests/release/test_original_member_bridge.py": 4}`. Each of
four sequential fresh workers collects the complete original file, then executes
one round-robin partition of that collection. This separates its four expensive
planner cases without changing a test ID, assertion, fixture, or product file.
The existing 180-second limit applies to each worker; the product's shared
120-second maximum is unchanged.

The parent requires identical complete collections and source hashes, the exact
disjoint cover of all collected IDs, and the ordinary complete setup/call/teardown
receipt for every partition. It emits the existing full-file aggregate receipt
only after all four succeed. Individual receipts and timings remain in the
artifact alongside that full-file receipt. A missing/duplicate partition, changed
collection/source, skip, missing phase, or nonzero exit fails the gate. Suites with
retired or unavailable nodes cannot be partitioned. Every logical suite remains explicitly classified in the manifest; the partition
mechanism does not change RUN/exclusion policy.

`next-offline.yml` runs Python 3.13.7 and 3.14.0 with the unchanged API dependency
hash lock and shared `next/requirements-ci-test.lock` for pytest and its tools.
This U3 tracking draft adds hash-locked Linux cryptography50.0.0 wheels to the existing supplemental lock; one venv installs the API/tool seed, then the exact U3 provider overlay and pip check. The production API lock is unchanged.
The existing numpy and psycopg fixture dependencies remain; tool pins are not duplicated. Matrix failures do not cancel the
other interpreter. The former `p1-install-oracle` job is absorbed here. Its original
47 method identities from five files are frozen in the existing consolidation
oracle, with provenance at absorption commit `6adde71d`. Every current install
test file still has to be in RUN, every current method identity must be unique,
and no current install method may be excluded. Additional install files and
methods run through this same gate; they do not redefine the original47 cohort.
The oracle checks a valid exclusion of a newer method independently of the
historical cohort and requires the same assertion to pass without that exclusion.
All actions use full commit pins, the
token has only `contents: read`, and checkout does not persist credentials.
The suite service uses a separate network namespace, denies IPv4/IPv6 socket
creation, and verifies those kernel restrictions before running any tests.
It has a 1 GiB memory ceiling, no swap, two CPUs, and no new privileges.
Dependency installation occurs before the offline service starts.

The C03 helper-location test retains isolated child imports (`-I`) and passes
`-B` explicitly because isolation ignores inherited `PYTHONDONTWRITEBYTECODE`.
Its return-code and shared-helper-location assertions remain unchanged. Cache
custody is checked against owned empty-cache source/dependency copies, with an
omitted-`-B` witness and a restored clean run; shared environments are not cleaned.

The imported-fixture proof remains conservative when several same-named modules
exist. A pinned source is not permission to select it instead of the module that
the worker imports. The same-class non-PG local-shadow controls refuse in both
imported-base and module-qualified-delegation forms; their no-shadow twins pass.
The ten runtime-grants/draft fixture declarations remain inventory RED pending
the separately reviewed actual-import proof. Their positive inventory controls
are retained, not waived by the P1 or cache-hygiene repairs.

The complete environment uses all three existing hash locks in the workflow:
`packages/api/requirements.lock` supplies `httpx` and `idna`,
`next/requirements-ci-test.lock` supplies the test tools, and
`.github/next-offline-requirements.lock` supplies numpy, psycopg and the exact U3
cryptography overlay. A partial reused driver environment or a parent-only
`sys.path` addition does not supply dependencies to reset/isolated child tests.
The supplemental cryptography hashes target Linux; a macOS diagnostic requires a
separately recorded native-wheel hash and does not qualify the Linux isolation
gate. Keep the tracked CI locks and kernel restriction checks unchanged.

The composed Gate B addition classifies 23 source suites as RUN and declares ten
real-PostgreSQL suites with the required owned-fixture evidence. The existing
conservative fixture reader currently rejects those ten declarations because
`from support import Fixture` has two source-path candidates. Their reasons do
not grant PASS credit or authorize native execution. The inventory remains RED
until that ambiguity is resolved through the offline-gate owner's separately
reviewed runner scope; merely listing the files does not accept an exclusion.

`.github/next-offline-suites.json` partitions every `test_*.py` and `*_test.py`
file recursively under `next/`. Entries are individual paths, not patterns.
An unlisted file, a stale or duplicated entry, a symlink, an empty RUN list,
or an exclusion without a one-line reason fails before test execution.
The workflow supplies the independent PR base SHA and checks out its history.
The standalone `--base-ref` default is the accepted a47a152b checkpoint.
The exact initial main `66cba6a1f8a2a3787844e168ab5415cb96738a0d` predates
this manifest. The named main bases `5627f7fb` and `abfc68ea` are also accepted bootstrap commits. Only for those existing, exact commits with the file absent, the
comparison uses the pinned a47a152b policy; `base_ref` and `policy_base_ref` both
appear in the receipt. Unknown/missing commits, malformed manifests, and any
other base without an inventory refuse. Once this PR lands, the normal PR base
manifest supplies the comparison. The manifest cannot supply its own approval. Every accepted-base RUN entry that leaves RUN is listed in the
`transitions` receipt and fails unless its exclusion has checkable fixture proof.
The RC0 oracle at `next/docs/runtime/rc0/test_oracle.py` is covered by this rule
and now runs alongside every current runtime/identity/factory/keyword/driver-free
and driver-privacy source suite. All 21 U3 release files are RUN. Live fixtures use one shared test-only workspace
helper with unique private mode-0700 directories under `output/r01u3-fixtures`.
Hosted cleanup receipts confirm an empty mode-0700 root and `NO_USERS_MAPPING`;
no `/Users` tmpfs or BindPaths mapping is used. The bridge retains all 41 original
IDs in four sequential execution partitions, as described above. At executable
head `af3d785e`, [offline run 38688671321](https://github.com/Kaidera-AI/cortex/actions/runs/38688671321)
attempts 1 and 2 passed on both Pythons: largest worker times were 36.638/28.135
seconds on 3.13.7 and 52.552/49.611 seconds on 3.14.0. Logical bridge totals across
the four workers were 142.561/109.012 and 204.161/192.522 seconds, respectively.
Each worker retains the 180-second limit; the product's shared 120-second maximum
is unchanged. Every hosted Python/attempt passed all 151 logical suite gates and
1,748 live tests, including all 646 U3 items; installed/native acceptance remains
separate.


To add a suite, review its fixture effects and add the file to `run`. A new or
changed whole-file exclusion must include `{kind, source, sha256, symbol}`
evidence. The existing AST reader verifies the exact source digest, a PostgreSQL
connect call in the named fixture's setup, and that every removed method's class
requires that fixture through an unconditional, resolved setup/inheritance chain,
including the existing module-qualified Core setup delegation.
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
The C11 availability files also stay mixed: three methods require real PostgreSQL
and one requires an actual IPv4 loopback server. Their exact named nodes carry
hashed fixture evidence in the same manifest. The dedicated outage file requires
the existing real-PG fixture throughout and has a whole-file exclusion. The
existing AST reader checks setup delegation, or the actual probe factory plus
server-side statement-timeout query, or the explicit loopback start_server call.
The mocked-connect deadline method receives an inert loopback DSN only to construct
its existing factory; its driver connect is replaced by the original test body.
The offline service still denies every actual IP socket. No real database is
started or inferred from these controls.

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

For ordinary live nodes, the unchanged PR155 validator checks a nonempty unique collection and exactly
one passing setup/call/teardown report per selected item. Skips, xfails, xpasses,
deselections, collection/setup/body/teardown errors, and nonzero raw pytest exits
fail. The parent additionally checks the actual child exit, the matching suite
and source hash, and that no selected node comes from another file. No per-file
test count is hardcoded: adding test methods to an admitted file runs them through
normal discovery. The summary and per-suite JSON/logs, including retirement baseline/mutation
receipts, are uploaded under distinct interpreter artifact names for seven days.
Every live collection must equal its passed methods; exact unavailable nodes and
retirements are separate NOT_RUN witnesses and receive no PASS credit. Frozen E1
expectations remain mandatory even when current product behavior is RED; no
waiver or xfail turns an unimplemented expectation into a green job.

The inventory-only check uses the standard library. The outcome oracle also
requires the shared hash-locked pytest tools for its real-worker controls:

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

Kai ruling359622c6-042a-4812-ad81-97c756d44864 declares exactly five frozen
E1 oracle nodes unavailable while E1 waits for RC3b (owner nemo). They use the
existing method_exclusions list with source-hash-bound unbuilt_unit evidence.
The exact authorized rows are pinned to policy commit741c43e4, not inferred from
a current failing test or an editable reason. Declarations may be removed when
E1 lands; adding or altering one requires another explicit policy ruling.

Unlike absent-fixture methods, unbuilt-unit nodes remain in actual pytest
collection and execution. Raw failed CALL reports and pytest exit1 remain in
the receipt. Passing setup/teardown and exactly one SurfaceAbsent/SemanticRed
assertion are required; any passing declaration, other exception, skip, xfail,
deselection, missing/duplicate phase or ordinary live failure refuses the gate.
The receipt's declared_unavailable witnesses always have pass_credit:0. A suite
consisting only of valid executed-unavailable nodes is allowed without inventing
a passing live test. All ordinary live phases still use unchanged U13.valid.
The worker and host call the same existing receipt_valid function. Summary
selected counts mean actual execution; live_collected excludes the five
executed-unavailable nodes. C06 remains mandatory live coverage (currently 43/43 PASS per Python).

The existing oracle runs real worker controls for an unbuilt assertion and a
fake now-passing declaration. The latter has raw pytest exit0 but gate/process
exit1 and zero PASS credit. Both control packets/logs are uploaded per Python.
