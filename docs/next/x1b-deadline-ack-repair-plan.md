# X1-b final deadline and ACK repair

Accepted scope: Kai handoff `5eb3cdcb-18f6-4007-99cd-5b9d874d3626`,
2026-10-16, one repair round capped at 1.5 hours. Reviewed branch base:
`6c64c0ba0be116b736f1d44153bc5af00b4e0a3b`, freshly verified on
`origin/cox/x1ab-build-20261016`; owned worktree clean before edits.
The row expressly accepts a common successful-exit guard covering every ACK.

Intent: close Vera `X1AB-001` and Hale `X1AB-ACK-001` at the final
successful return, retaining the original operation deadline, durable ordinary
facts and one-consumption/no-replay behavior. A final current-owner/history read
can currently return after the retained local process stop. The dispatch seam
has the same after-observation deadline gap.

Plan and order:

1. Re-run the unchanged reviewers' three Vera cutoff witnesses, four Hale ACK
   witnesses (simulated and actual elapsed time) and paired controls against the
   base. Preserve commands, exact test IDs, assertion failures and source hashes.
2. Add focused regressions in
   `next/tests/install/test_lifecycle_ack_deadline.py`, using the existing
   HOLD-1 Subject, non-SQL recorder and AppliedSubject fixtures. Cover delayed
   dispatch, final result observation and child history reads, actual elapsed
   time, short-delay positives, durable truth/no replay and exception cleanup.
   Commit these failing tests and this accepted plan before product changes.
3. Reuse `_History._check_publication` in
   `next/src/cortex_core/install/lifecycle.py`: on successful exit from a
   deadline-bound `operation`, before restoring its original scope; and after
   all potentially waiting dispatch preparation, immediately before dispatch.
   Keep original exceptions unchanged and preserve deadline-free private
   recorders. No new deadline, clock, verifier, supplier, parser or wire format.
4. Run the same unchanged reviewer bodies and product regressions on CPython
   3.13.7 and 3.14.0. Run the original ten-module X1-a/b regression selection
   plus the new module; compare all failure IDs and missing-corpus diagnostics
   with the baseline. A full-suite exit 1 is not green if inherited authentic
   corpus is absent; disclose every such failure and every new difference.
5. Self-review the complete delta, preserve original tests and unrelated bytes,
   commit the fix and push only `cox/x1ab-build-20261016`. Verify destination
   SHA. If a PR exists, require its exact-head hosted checks and record run IDs;
   otherwise report no PR/hosted run. Return the same row for Vera/Hale round 2.

Solo depth tree: reproduction -> committed RED regressions -> existing-guard
repair -> both-Python regression comparison -> branch push/readback -> same-row
return. No external judgment service or delegation. Evidence home:
`output/Cortex/cox-x1ab-ack-fix-20261016` under the Helix root.

Excluded: X1-a redesign, X1-c/d, recovery inner-lock deadline propagation,
installed X2/X7 suppliers, live hosts/databases, native qualification, PR
creation, merge, deployment and release. Independent reviewers own closure;
this author supplies RED-to-GREEN evidence and proposes the disposition only.

Risks: a successful-exit check must run after all ACK-tail reads without
masking an existing exception; late refusal must retain the original durable
result and must not replay the effect. No synchronous guard preempts a wait
that never returns; the separately gated supervision obligation stays open.
