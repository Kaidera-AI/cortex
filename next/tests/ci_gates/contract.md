# CI control field map

All JSON files use the existing strict duplicate-key/nonfinite refusal discipline.
File inputs are host-owned project evidence, not service requests. Raw files and
reviewed inputs must stay immutable. Absolute paths belong in evidence packets;
repository-relative names belong in source/spec identities. Exit zero admits only
the named evidence layer. Refusals exit nonzero; no retry or waiver is automatic.

## Composition

`merged_tree(repository, expected, receipts)` checks real Git objects.
COMPOSITION_PACKET contains `repository`, `expected`, `receipts`.
Expected fields: full 40-hex `base`, `head`, `composition`, `tree`; nonempty unique
`lanes`. Composition must have ordered parents [base, head], base must be an
ancestor of head, and its actual tree must equal the independent reviewed tree.
Each required lane appears once, with `lane`, `source_commit=composition`, `tree`.
A PR-head receipt cannot stand in for composed-tree execution. The lead supplies the
independent pins/required lanes; this helper does not discover or approve that policy.

## History

COLLECTION_PACKET fields: `repository` (owner/repository), full `head`,
`output_directory` (fresh absolute directory). Collection stores all repository
workflow runs, every attempt's detail and every job page, including older heads and
failed/cancelled/timed-out runs. It never filters by latest conclusion. Initial and
final provider run inventories must match; changed snapshots refuse and retain the
partial raw files. No automatic retry. GitHub retention/deletion is an external
limit: the lead must compare prior accepted archives and known run IDs; an absent
provider record is never proof that an earlier failure did not exist.

HISTORY_PACKET fields: `index` (absolute collected index.json), `expected_head`,
`required_jobs` (workflow name -> nonempty unique complete job-context-name list),
`accepted_gate`:
- `commit`: last accepted gate's full source commit;
- `accepted_at`: ISO timestamp with timezone;
- `artifact`: absolute independently reviewed acceptance receipt;
- `receipt_sha256`: hash of that receipt.

The CLI verifies acceptance-artifact bytes. The accountable lead authenticates its
meaning/authority and the required contexts; a file hash alone grants no approval.
`history_complete(index, expected_head, required_jobs)` is also a pure metadata
interface; unit fixtures may omit raw artifacts and are labelled metadata only.
Production CLI history admission requires raw provider artifacts and binds every
parsed run/job/attempt back to those exact bytes, including the final snapshot.
The independent accepted-gate cutoff is supplied through `index.accepted_gate`
by the CLI. All raw history stays present; runs updated since cutoff are active
obligations. An old run rerun after cutoff is active, even if its creation is older.

Every listed run/attempt/job must be complete, nonempty and identity-consistent.
Each required workflow needs two distinct run IDs at the final head, with all
required contexts successful, and no rerun attempts substituting for independent
runs. All jobs, including extra contexts, must be successful. Earlier red in the
active window blocks unless an explicit closure references its run/attempt/job,
`fixed_in` final head, `root_cause`, `reviewer`, `evidence_sha256`. A run-level red
uses job_id "run". Current-head red cannot be closed by metadata. Closure references
still need lead review; they are not machine-authenticated judgments. This
conservative collector includes unrelated repository runs too; the lead may need
their accepted closures. It never silently excludes a failure as irrelevant.

No current Day-1 branch hosted run is invented. Two local unit runs do not satisfy
the two hosted runs required before a later GO.

## Causal pilot

ONE SPEC PER SOURCE FILE. The checked set is all changed Python source/tool/test
files plus the retained floor profile JSON. Specs themselves are evidence metadata.
Each JSON spec has `source`, `source_sha256`, `tests` (relative file -> SHA256),
`expected_nodes` (nonempty exact unittest IDs), `mutation.before`, `mutation.after`.
Extra `kind`/`invariant` fields explain the obligation. Literal mutation anchors
must occur exactly once and alter bytes. Test-fixture calibration is labelled
oracle-fixture; it is not a product-guard kill.

The pilot archives the committed tracked tree into a separate disposable case per
spec. It runs healthy -> observed mutation -> restored healthy, in fresh child
processes without bytecode reuse. It retains actual mutant bytes, source-commit
identity, commands/cwd/exits, stdout/stderr and hashes. The existing runner attributes
AssertionError to BODY versus fixtures. A loaded-source fingerprint comes from the
module actually imported, or the exact PROFILE data used by the module. Missing
import/collection, extra/missing nodes, skipped/expected-failure/unexpected-success
tests, runner errors, crashes and timeouts are inconclusive. They get zero kill
credit. No stdout marker is interpreted as an attestation from a hostile process:
the collector and disposable test environment remain host-owned.

`mutation_complete(root, changed_sources, specs, runs)` checks the exact bijection,
original/test/spec hashes, computed-versus-observed mutant bytes and raw-log hashes.
Every process must report the exact expected selected set and observed source hash;
only intended named BODY AssertionError earns kill credit. Healthy/restored must
be complete clean runs. It does not accept another author's missing obligations.

MUTATION_PACKET contains `root`, independently reviewed `base`, `changed_sources`,
absolute spec paths in `specs`, absolute run packet paths in `runs`.
The CLI compares the source list against actual Git diff from base to HEAD;
include `next/tests/floor/profile.json` as well as changed Python files.
The pilot's admission.json needs the independent base added for this CLI check.
Use an accepted clean tip; metadata admission is not review or merge permission.
