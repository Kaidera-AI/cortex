# E0 review delta: origin and elapsed deadline

Kai 3dddaf64; reviewed base c87306ccaacba64139607dc3d3709b00016ab624.
Tests-first freeze 9651f922b091d18ff094595813201d52fac40039 precedes this
refreeze. Mike owns independent finding closure; this is an author repair.

E0-ORIGIN-001: the existing permissive-client redirect method records the actual
HTTP request and independently requires exactly one POST, scheme https, host
openrouter.ai, effective port443 and raw path /api/v1/embeddings. Its expectation
imports no product endpoint/policy constant. Raw path also excludes added query
parameters. The existing synthetic request content recording is retained.

E0-DEADLINE-002: the existing forwarding method keeps its original argument
assertion and additionally refuses successful completion at/after the supplied
absolute deadline. Seven added nodes in test_e0_expiry.py exercise selection gate
post-callback expiry under both a shorter deadline and a clamped10-second maximum;
read expiry at cache return, provider-miss return within the synthetic cache,
vector return, freshness return and final authorization return. Each clock moves
virtually to deadline+1; asyncio.sleep(0) yields without a wall-clock delay.
Expired work must produce no published success and no subsequent stage dispatch.
The provider case models a late provider result at the cache seam; it does not
invoke real hosted transport or qualify real cache publication/authorization.

Fresh before/after replay of Mike's exact five static scenarios reproduces his
three kills/two survivors at the reviewed bytes, then five kills/zero survivors
at the repaired bytes. In particular his unchanged wrong-origin and late-success
outcomes now each cause one deciding BODY assertion. Eight additional static
controls cover wrong method, gate post-await checks, gate budget extension and
the five read stages; golden/restoration pass, all8 wrong outcomes are killed.
These are assertion-sensitivity controls with synthetic dependency outcomes,
not causal product mutations or future E1/E2 implementation qualification.

Full current oracle:46 nodes =29 surface-absence RED +12 semantic RED +5 PASS;
zero errors/skips/effect attempts. Two added gate nodes stop at missing selection
surface; five added read nodes expose actual current expiry mismatches. Every
original39 node remains; every prior outcome remains the same. Inherited148
tests pass (8 RC0 +106 RC2 +8 identity +20 RC3a +6 provider). Mike's replay confirms
invoked product code matches fresh compilation in all six suites.

Only two existing definition spans changed in test_e0.py. All other37 methods in
that file and all bytes outside those spans are exact; run_oracle.py,
test_e0_join.py, test_e0_cache_fence.py and README.md are unchanged. No accepted
RC0 bytes, fixture, pin, shared receipt classifier, product source or schema was
edited. POLICY.md, PLAN.md, GATES.md and the unapplied RC0-E0-001.patch retain
their prior bytes; their39-node receipts are historical. FIX_FREEZE.json binds
the current46-node oracle. RC0 ratification/fixture-pin coordination, C11 owner
mapping and all original effect/native gates remain outside this tests-only row.

Evidence: output/Cortex/nemo-e0-fix-20261014. BEFORE/ and MIKE/ carry original
and repaired static receipts; ADDITIONAL/ carries golden/wrong/restored stage
receipts. SCOPE_AUDIT.json lists exact definition hashes; oracle.json and the
five inherited suite packets contain fresh compiled-code/effect receipts. The
initial extra-control restoration comparison used absolute virtual timestamps;
its uncredited preflight receipts are retained, then the driver compares relative
budget observations. Frozen test bytes were unchanged throughout that correction.

LEAN: reused read_port, existing policy ports, run_oracle/test_receipts and Mike's
static/replay drivers; did NOT add product code, config, parser, verifier or API;
could delete or merge none. No provider spend, real IP/DNS/PG/process test effect,
PR/main/force, subagent, Gavel or Jev. Own-branch normal Git transport is authorized.
