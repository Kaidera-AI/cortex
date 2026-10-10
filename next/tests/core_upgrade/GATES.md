# Core upgrade unit gates

- [ ] Actual released v0.1.002→new signed aggregate pair is present and
  approved by Ren-TK/Kai; until then production admission refuses.
- [ ] Signature, byte digest, schema/event, downgrade, platform and module
  mismatches refuse before DB change under fail-capable RED/GREEN tests.
- [ ] Explicit migration port applies only the named expand-only prefix;
  interruption/re-entry at every step does not duplicate rows or events.
- [ ] Canonical bytes/digests, permissions, cursors and model identity compare
  equal before/after on disposable real PostgreSQL with cleanup.
- [ ] Pre-acceptance rollback calls verified restore/fence; the durable
  acceptance point forbids rollback and requires fix-forward.
- [ ] Source mutations die in expected test bodies, raw exact-head evidence
  exists, and H-D471 merge/adoption remains held.
