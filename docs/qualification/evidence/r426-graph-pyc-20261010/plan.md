# H-D449 graph builder bytecode successor

Authority: Kai's 2026-10-10 09:04 DO NOW names this exact repair, choice, RED/GREEN and feature PR. Cortex artifact rule pre-approves implementation plans. Base is explicitly pinned to PR38 e9a19f1476d1f6f3be14d7df6ccb3fb7eddd9e7e. Own branch cox/r426-graph-pyc-20261010; clean before edit; all shared checkouts are read-only.

Intent/spec: the retained attempt5 classification proves 493 venv .pyc payloads have timestamp-mode headers. Before the builder COPY commits, every generated /opt/bcrg cache must use checked-hash invalidation and fixed hash seed. Preserve paths, source, runtime identity, locks, dependencies, model pins, cleanup ownership and final compilation.

Chosen mechanism: preserve the existing pip --no-compile calls and extend explicit checked-hash compilation into the builder's existing fetch/cleanup RUN, after imports and before its layer commits. One compileall command uses the same checked-hash policy, hash seed and final /opt/bcrg paths as the existing finalizer. Imports can generate timestamp caches even when pip compilation is disabled; forcing compilation at this boundary replaces them. No SOURCE_DATE_EPOCH mechanism is added.

Order and scope:
1. Freeze a new native Python regression in packages/containers/graph-worker/tests/test_builder_pyc.py; preserve all existing tests byte-for-byte.
2. Run it with all eight unchanged finalizer/ownership/guard cases, in one copied bounded Python3.13.15 container only after C06 fixture absence. The new body creates real timestamp caches under /opt/bcrg, executes compiler fragments extracted from the real builder RUN, checks every header and compares two different source mtimes. RED must be its BODY assertion, with eight predecessor cases GREEN and no errors.
3. Change ONLY production Dockerfile: add one checked-hash compileall fragment after builder cleanup and before /build removal. Re-run unchanged nine cases, actually remove the new fragment as a mutation and prove the new body fails, then restore and re-run.
4. Verify the graph delivery surface is otherwise byte-identical, capture exact copied inventory, cleanup absence, final source SHA and a fresh verifier. Push one feature PR for Vera. No main merge, native x86 graph rebuild, install, signature or producer reuse; ren-kos owns attempt6 after acceptance.

Proof controller and immutable receipts live beside this plan. Canonical receipts fold into root output/Cortex/design55-r426/2026-10-10/graph-pyc; publication copies are byte-exact. The controller is new for this named fix, uses pending-before-create, fresh nonce/owner/name/immutable ID, bounded removal retries and keeps its fcntl lock until verified absence. Never launch alongside an active cox fixture; resource bound is 2 CPU/1 GiB, network none, no ports/binds. Failure preserves a durable dirty marker and blocks later acquire.

Risk: a source-level compiler regression cannot certify a full graph image, intermediate OCI layers, x86_64 or real model build. Those gates stay with Vera/ren-kos. A late final-stage recompile alone would leave the builder COPY content nondeterministic; this fix acts before that COPY. Door two-way; blast graph.
