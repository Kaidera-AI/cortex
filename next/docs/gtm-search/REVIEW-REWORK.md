# PR29 review rework

Mike's B01-PR29-1..4 are proposed fixed; only Mike closes them.

```text
vector input -> canonical float32 -> representable components + finite NORMAL cosine norm
authorized operation -> whole PG transaction -> serialization conflict
  -> rollback -> fresh snapshot + current Core grant (three attempts maximum)
  -> typed capability_unavailable / concurrent_update on exhaustion
mutation runner -> clean full baseline -> expected named assertion -> restored full suite
container runner -> TCP readiness -> checks -> removal AND empty inventory
```

RED commits: e84ea2e retains the zero-vector/NaN probe and reproduces the four findings;
5e80817 rejects unrelated mutation failures; f90ae1e catches temporary-init-server readiness.
The actual readiness race is retained as INCONCLUSIVE, not a semantic kill.

Run from this worktree with Python 3.12 and the pinned search-requirements.txt:

```sh
.venv/bin/python next/tests/integration/run_pg_search.py
.venv/bin/python next/tests/integration/mutate_pg_search.py /tmp/nemo-pr29-mutations
```

Local full baseline/restoration after round3: 27 checks. Nine named source/SQL mutants fail their
expected assertions. Raw logs and machine receipts are in helix docs lane-F:
pr29-review-red.log, pr29-mutation-classifier-red.log, pr29-readiness-red.log,
pr29-review-mutations/ (includes the inconclusive setup run), and
pr29-review-mutations-final/ (baseline, per-mutant output, restoration, JSON).
The cleanup FAIL printed inside the negative mocked tool check is expected; the
runner's final cleanup marker is PASS. Every actual container is removed.

Round3: Mike reopened B01-PR29-1 on ab84b7e. RED67b5963 retains his collinear probe:
stored [1e-22]+zeros, query [2.7e-23]+zeros produced distance0.2440710539815456.
Canonical float32 norm must now be >=2**-126 (smallest normal float32); subnormal
storage/query vectors are refused. The exact floor is admitted and tested with
collinear vectors. No normalization or embedding identity change is introduced.
Round3 raw RED/GREEN/nine-mutant receipts are lane-F/pr29-subnormal-red.log,
pr29-subnormal-mutations/ and pr29-subnormal-final-head-green.log. The added floor
rollback mutant fails Mike's exact zero-distance assertion, never a setup error.

The Core writer's note_revision_in_transaction hook deliberately never retries an
isolated statement: Cox must retry the entire enclosing authorized Core transaction.
The adapter's standalone PG operations retry only serialization conflicts, not
permission denial, invalid input, stale embeddings or Core outage.

Door: two-way. Blast radius: search. No migration/schema change in this rework.
Synthetic development evidence only; C04/C07/C11 integration and Mike's production
recall/latency checks remain separate gates. No live credential or API was used.
