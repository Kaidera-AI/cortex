# PR31 review rework

```text
resolver / HTTP dependency exception (typed or untyped) -> fixed scrubbed refusal
cache caller budget -> PG scope-lock lookup -> provider -> PG publication
  -> check deadline before every successful return
cancel/failure -> claim release <= min(50ms, remaining caller budget), else lease expiry
```

B01-PR31-1/2 are proposed fixed; only the reviewer closes them. Finding2 is a
caller DEADLINE breach (Kai22:31 correction), not serving a stale TTL entry.
The TTL predicate already runs after the scope lock; an added real-PG test verifies
that an entry expiring during that wait is recomputed within a longer caller budget.

RED7b7fcbc retains Mike's native 20ms/120ms advisory-lock probe and typed-resolver
sentinel. Additional RED assertions cover a still-held lock, late successful lookup,
publication authorization, bounded cancellation cleanup and typed HTTP exceptions.
Raw before output: helix docs lane-F/pr31-findings-red-full.log (62 checks, seven
assertion failures). Midflight permission-generation refusal is also retained.

After: 63 full-stack checks; 12 expected-assertion semantic mutants, with clean full
baseline and restored suite. Raw per-mutant target/status JSON and outputs:
lane-F/pr31-findings-mutations/. Final-head output: pr31-findings-final-head-green.log.

```sh
.venv/bin/python next/tests/integration/run_query_cache.py
.venv/bin/python next/tests/integration/mutate_query_cache.py .worktrees/nemo-p01-mutation-proof
```

Cancellation is never converted to successful output or swallowed by release;
release failures leave an expiring fenced claim. No connection is held across the
provider await. Permission generation and grant checks remain current at publish.
The hosted boundary forwards only its locally generated fixed reasons; dependency
messages, even already-typed ones, never become outward reason or exception text.

No schema/provider/credential store changes. P01 remains the existing OpenRouter
adapter and disposable PG query cache. C04/C11 and production workload acceptance
remain separate gates. Tests use synthetic credentials/transport and one bounded
disposable PG; no live service call. Door: two-way. Blast radius: retrieval.
