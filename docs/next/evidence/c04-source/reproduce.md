# C04 authorization proof custody

Canonical plan: [C04 implementation plan](../../C04_IMPLEMENTATION_PLAN.md). Product repository is `helix/cortex`; the Helix root repository is a different product. Use explicit `-R Kaidera-AI/cortex` for every forge operation.

## Boundaries

This slice qualifies the private synchronous Core authorization context, additive auth-0002 migration, forced RLS and shared negative fixture. The server owns the connection and its top-level transaction; callers keep results private until successful context exit. Clients receive neither SQL access nor engine/database credentials. Manual SQL transaction termination is outside that private connection contract. HTTP routes, engine tenant filters/private networking, installer provisioning, native amd64 and release acceptance remain later gates. Only fresh disposable Podman resources are used here; no live services, ports or host dependency installs.

The immutable accepted tuple is held in a verifier-owned temporary relation, with explicit ACLs, owner/type/persistence/namespace validation and a once-only transaction advisory marker. RLS requires current request settings to match that tuple and resolves current authority again. The marker survives DISCARD TEMP and session unlock; destroying temporary state denies access and cannot authorize a wider rebind. A nonblocking acquisition avoids waiting on a foreign marker. The four canonical schemas and28 existing business tables remain unchanged in count; all are forced RLS.

PostgreSQL's [SECURITY DEFINER guidance](https://www.postgresql.org/docs/18/sql-createfunction.html) explains fixed search paths and explicit PUBLIC revocation. [DISCARD](https://www.postgresql.org/docs/18/sql-discard.html) and [transaction advisory locks](https://www.postgresql.org/docs/18/explicit-locking.html#ADVISORY-LOCKS) informed the separate erased-state control. Actual PG18.4 execution supplies the behavior receipts.

## Before and after

`context-auth-red.json` retains the original missing-port RED with the final25 original tests. Earlier fixture diagnostics are retained; the pre-implementation count correction was to exactly28 canonical business tables, not weakened authorization behavior. `auth-attempt-001.json` is baseline-only historical qualification.

`consumer-missing-red.json` has two frozen caller tests/five missing-adapter errors, explicitly an operational missing-implementation RED, not a mutation kill. `consumer-green.json` retains the first ambiguous recipe preflight failure. `consumer-green-attempt-002.json` proves two clean tests, an actual exit-code-only adapter fault with five expected-body assertions/zero errors, and restored two clean tests.

`auth-green-attempt-001.json` at3c9fee7 has30 schema/27 auth/16 actual expected-body mutants/restored source and removed stack. The independent verifier then identified temporary scope changes restored before exit and NULL SQL action, so that proof does not cover them. `binding-red.json` at e341698 preserves all five real behavior failures as expected-body assertions with zero errors.

`binding-green-attempt-001.json` at13b67f2 passes30 schema/32 auth but correctly fails mutation qualification: two old guard faults survived because the new private binding masked their former effects. Nothing is deleted or relabeled. New independent guard probes inspect exit refusal without DML and session credentials/all seven settings directly. `private-red.json` at b1dcc3d has39 auth/two NULL-target subtest assertions/zero errors; other private custody controls pass. All first-added test bytes are retained through the subsequent product fixes. The final gate targets30 schema/40 auth/22 actual expected-body mutants and restored source; final success is recorded only in its completed JSON and independent verifier receipt.

## Reproduction

Keep every published receipt immutable and hash-verify it first. Reproduce into a fresh evidence directory and unique phase filename, with a separate new index; do not run a recipe in a published directory and overwrite any member.

Use an isolated checkout of the published product head under Helix `.worktrees`, copy the previously pinned three cp312/arm64 offline wheels into its ignored `tmp/wheels`, and verify wheel hashes against the original receipts. Required locally cached image IDs are PG18.4 `sha256:db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d` and Python3.12 `sha256:ce9a404c2c0138e747a43e6ea022d2f7e670ed868df35d627a663ba7fb940ea9`. The controller checks architecture. Each reproduction owns one stack,2CPU/1GiB total, read-only nonroot containers, ephemeral tmpfs, networknone/private loopback, no ports or bind mounts. Source and dependencies are copied; pip runs offline inside the fixture only.

The C04-only `run-auth-pg.py` prepares output before effect, records a fresh lifecycle before creating each resource, reconciles exact labels/name/ID, removes only verified immutable IDs, and retains failed reconciliation for retry. A pending/foreign child blocks pod deletion. It captures the product HEAD and all copied source hashes before launch, runs the full schema/auth suites and actual mutator, verifies every restored copied byte, then removes and checks resources. The frozen C03/graph replay producers and historical C04 diagnostics are not imported or reused; their separate repair hold remains in force. This C04 fixture does not constitute their lifecycle repair acceptance.

Run the controller with the checkout as cwd and a fresh nonreserved phase name. The two reserved RED modes reproduce earlier source only; their expected exact fail sets are encoded. `consumer-missing-red` likewise requires its original before-adapter source. For a current candidate, use `run-auth-receipts.py` with a unique GREEN phase. Execute full contract/shared gates sequentially after the PG stack is gone. The final source/receipt index names all commands and hashes. Any errors, unexpected assertion, survivor, cleanup failure or source mismatch blocks qualification.

## Open gates

Author internal verification is separate from Vera source review and Kai/CTO disposition. No self-approval, main merge, live migration, image signing or release follows from these receipts. Root folder fitness has the existing unrelated dist/packaging RED; do not claim the estate globally passes. C03's exact merged-SHA replay is still owed after Mike's cleanup pattern is accepted and applied; C04 receipts do not satisfy that gate.
