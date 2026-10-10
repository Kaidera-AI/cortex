# H-D449 producer epoch argv successor — accepted10:26/10:30

Source basecb99896f pinned; product graph9d509a3d unchanged.
Production change: REMOVE SOURCE_DATE_EPOCH build arg; KEEP fixed --timestamp
1791586380, all other argv, roles, contexts, identity, stores, arch, verifier,
source checkout normalization, existing admission and downstream gates.

Kai10:30 ratifies one test assertion supersession: old test bytes archived with
historicalbaseline2PASS; assertIn epoch build arg becomes assertNotIn. Amended
baseline1expectedBODY RED/1PASS before producer fix; current amended2PASS. Other
existing tests unchanged; no skip, deleted assertion or hidden monkeypatch.
The separate new seven-role regression was committedRED before fix.

Add native_argv_preflight.py: native unprivilegedLinuxx86_64 actualdistroPodman5.8.2,
all7 complete real make_plan argv; mirrored fixture recipe/context paths, actual
FROMscratch+oneCOPY bytes, empty private store/auth, no network/input credential,
bounded commands and verified owned-image/container cleanup. Baseline must give
125ambiguity everyrole; candidate0 everyrole with actual OCI config/history/COPY
clock1791586380 and COPYbytes/mode. Receipt pins code/engine/source/host/fullargv.
Native proof is parser/clock qualification only, never fullartifact determinism.

External require-guard-controls candidate binds driver/expected inputs and
RED-first shell check into fresh14; native_argv=True gate refuses missing/stale/
RED/wrong-host/source/engine receipt before launcher/each actualbuild. Phase-only
controls/host preparation do not start product builds. A fresh same-host native
receipt is required for realbuilds, not a receipt reused from the proof host.

One proof-only fresh admittedAMI/t3.xlarge/encrypted80GiB strictSSH host approved
byKai10:30, max90min, tariffestimate<=0.30. Tinyfixturesonly; fullattempt6 waits
VeraACCEPT andanotherfreshhost. Retain all safe evidence and exact retirement.

Scope owned source: producer, one amended originaltest, appended newregression,
nativehelper, thisplan. Other5producerfiles/allotherexistingdeploypathsunchanged.
Source/codecommit before native proof, then verifiedfeature-origin publish and
receiptfold; no mainmerge, signature, install or fullgraph build in this change.
