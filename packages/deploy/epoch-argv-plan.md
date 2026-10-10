# H-D44910:26 producer argv successor

To: kai; owner ren-kos. Accepted task: REMOVE SOURCE_DATE_EPOCH build arg only,
KEEP --timestamp1791586380 and checkout normalization. Source basecb99896f pinned,
graph9d509a3d unchanged. Original tests remain byte-frozen.

Owned source change: packages/deploy/build-manual-linux.py one dictionary entry
removal and corresponding misleading comment correction; add new RED-first
regression test/test driver and native argv preflight gate/runner in deploy tooling;
plan documents binding. All other existing deploy files remain exact.
Native driver consumes real make_plan output for all7roles, including every flag,
build arg, tag, architecture and recipe/context spelling. Isolated fixture root
mirrors all context/recipe paths; each recipe is FROM scratch +one COPY. Only
fixture data/recipe and private storage differ from real workloads; no parser flag
subset, environment injection or archive rewrite. UID1000 nativeLinuxx86_64,
actual/usr/bin/podman5.8.2, private empty engine store/auth, no credential/network
input, per-command timeout, inspect/archive checks, exact-ID cleanup before return.
Baseline must give125 ambiguity, successor0 and fixed image/file timestamps.
Gate verifies native receipt, actual producer/driver hashes/complete argv/current
engine/version/sourceSha and current controls before every real build. Fresh
controls bind driver/contract; native receipt generated before real start.

One issue to adjudicate before claiming GREEN: frozen test_producer_epoch.py
requires SOURCE_DATE_EPOCH=1791586380 in every argv. It necessarily fails under
new instruction. Preserve that file/body; reproduce historical baselinePASS and
new named assertionRED, add new current-rule tests, and request explicit obsolete
assertion supersession rather than edit/skip/monkeypatch it silently.

Native verification requires a fresh disposable host because attempt6 host was
retired correctly. Proposed same admittedAMI/t3.xlarge/encrypted80GiB/strict SSH,
proof-only tiny fixture, no fullgraph, TTLmax90min and tariff estimate<=0.30.
Need Kai confirm this proof-host execution envelope; full attempt6 remains after
VeraACCEPT on a separate fresh host. Host creation not yet run.

Order: plan/consult -> source worktree; committed new regressionRED againstcb
before fix -> minimalfix+portable receipts -> native baseline125/new0 realfixture
-> custody+host/EBSretirement -> source/receiptreviewVera -> fresh attempt6.
Any native preflight failure remainsRED and stops fullbuild. No mainmerge/signing.
