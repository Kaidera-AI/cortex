# Core upgrade unit gates

- [ ] Actual released v0.1.002→new signed aggregate pair is present and
  approved by Ren-TK/Kai; the released partial has no such aggregate. Until
  then production admission refuses and synthetic manifests remain test-only.
- [x] Signature, byte digest, schema/event, downgrade, platform and module
  mismatches refuse before DB change under fail-capable RED/GREEN tests.
- [x] Explicit migration port applies only the named expand-only prefix;
  interruption/re-entry at every step does not duplicate rows or events.
- [x] Canonical bytes/digests, permissions, cursors and model identity compare
  equal before/after on disposable real PostgreSQL with cleanup.
- [x] Pre-acceptance rollback calls verified restore/fence; the durable
  acceptance point forbids rollback and requires fix-forward.
- [x] Source mutations die in expected test bodies and raw exact-head
  evidence exists. H-D471 merge/adoption remains held.
