# C01 source implementation plan — H-D455

Owner: cox@helix; reviewer: mike@helix; adjudicator: kai@helix. 2026-10-09. BUILD under the accepted plan and CTO H-D455 code go. H-D455 supersedes the document-only hold and removes optional engines from GTM; their contract slots remain explicit. No release or merge authorization is inferred.

Source repository: `Kaidera-AI/cortex`. Base: `origin/main` at `04a26d0bf40c07ed3082328d3cc42c5a72ca8411`, freshly fetched. Own SSD worktree: `.worktrees/cox-c01-contracts-20261009`, task branch `cox/c01-contracts-20261009`. [Pre-edit receipt](evidence/c01-source/pre-edit.json) records all 76 recent refs, 62 unrelated/no-merge-base refs resolved by full-tree path inspection, worktree inventory and shared checkout state. No declared C01 path overlaps another worker. Existing untracked v2/archive material is left untouched.

## Files and order

1. Commit this plan and failing contract tests in `next/tests/contract/test_contracts.py`; pinned test dependencies in `next/requirements-test.txt`. Run RED in a fresh, bounded Podman container, copying inputs; no host mounts or published ports.
2. Carry the accepted draft byte-for-byte into `next/contracts/openapi.json` and its route matrix into `next/contracts/route-matrix.md`, with a source provenance manifest. It remains a source snapshot and explicit C02 candidate, not released-consumer proof.
3. Add `next/contracts/outbox-event.schema.json`, upsert/delete envelope fixtures, and small write-receipt/capability/error fixtures. Envelope v1 binds event/installation/tenant/project/aggregate IDs, aggregate kind/revision, operation/tombstone, schema version, payload reference/digest and occurrence time. Publication cursor belongs to the committed feed, not allocated event IDs.
4. Add `next/src/cortex_core/contracts.py` to validate envelopes and proposed v2 wire fixtures. Add a versioned schema index and bounded mutation verifier in `next/scripts/mutate_contracts.py`. No server, database or engine implementation enters C01.
5. Run the unchanged full contract suite GREEN and mutations against each implementation/schema source. Capture exit codes and literal output. A fresh-context verifier checks this seam; Mike's independent review and Kai's acceptance remain outstanding.
6. Fetch/integrate the target line, rerun checks, push only the task branch, create one C01 PR, write one return and continue to C03. Never self-merge or alter v0.1.003. Copy plan/evidence into the canonical helix docs home the same day.

## Risks and receipts

- C02 waits for released v0.1.003/KOS pins: preserve permissive candidates and explicit freeze gaps; do not normalize them into a compatibility claim.
- The envelope is an I0 v1 seam; C03/C06 must enforce transactionality, immutable identities, monotonic per-aggregate revision and commit-safe delivery. C01 proves validation only.
- Tests include missing/invalid scope and IDs, revision/schema bounds, delete/tombstone mismatch, unknown fields, malformed payload digests, rejected fake errors, ready-empty versus unavailable/partial outcomes and unacknowledged write receipts.
- Run only on native Linux arm64 in the Mac Podman VM. This is not Linux x86_64 release qualification. At most one owned disposable test stack, CPU 2 / memory 1 GiB, fresh writable tmpfs, no network during checks after dependency setup, removed with a receipt.
- Infra naming: PASS; `kaidera-test-contract-1`, test singleton/fleet slot, helix/Cortex, contract verification role. No cloud resource or live service is created. The test image is upstream Python slim/glibc, pinned by its inspected arm64 digest; dependency versions are explicit and the runner does not ship.
- Correct the previously routed CXV2-013 document hook to “rollback before acceptance, fix forward after”; keep this clarity fix separate from implementation proof.

Verification: `python -m unittest discover -s next/tests/contract -v`; `python next/scripts/mutate_contracts.py`. RED must be a missing seam, not an environment/import dependency failure. GREEN runs the same test bytes. Mutation survivors fail the verifier. Root `scripts/fitness/check-folder-structure.sh` and `git diff --check` cover the authored paths; existing shared-tree defects are reported separately.
