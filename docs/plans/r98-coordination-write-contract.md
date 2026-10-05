# R98 coordination and write-fence contract

Owner: ren-cx@helix. Current authority: /Users/amadmalik/DevVault/helix/docs/handoffs/INBOX_ren-cx.md (r100 DO NOW, r98 task) and /Users/amadmalik/DevVault/helix/docs/handoffs/2026-10-05_kai_rulings-r98.md, within the accepted R24 L2 plan in /Users/amadmalik/DevVault/helix/Program/Cortex/local-deployment/PLAN_2026-10-03.md. PR #9 is Vera ACCEPTED. Gavel/Jev remains held.

Source worktree: /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r98-coordination-write-contract-20261005; locked, clean, based on freshly verified origin fe23fd18538b2a651ff4835437abbb75944cbc4e. SSD UUID 8071CB26-B817-496E-A1FC-A8FE9A87C184 checked; host memory 51 percent free. Public source and unissued in-memory fixtures only.

## One bounded contract

The native registry already supplies coordination.handoff write operations, strict request models, claim generation fences, server identity checks and transactional receipts. This slice uses those models in the member client before private key access. It does not invent legacy-to-native business semantics.

| Surface | Contract |
|---|---|
| Native handoff create/claim/renew/release/return/accept/rework/retry/fail/abandon/withdraw | Explicit caller idempotency key: nonempty, at most 128 characters, safe HTTP field value. Path IDs are UUIDs, never prefixes or path fragments. The registry request model validates the original body, without replacing it with inferred fields or defaults. |
| Native renew/release/return/fail/abandon | Caller supplies a strict positive expected_claim_generation. Missing, bool, string, zero, negative, foreign identity/scope fields and invalid bodies fail before reader/transport. A well-formed but stale generation reaches the authoritative server unchanged; the client never fetches or repairs it. |
| Native successful writes | Require a committed receipt for the requested operation, matching handoff ID where applicable, valid scope UUID, status, strict generation, revision and policy revision. An invalid 2xx response is an explicit uncertain-outcome error, never empty success; no automatic retry. Preserve valid data, replay, request ID and credential expiry. |
| Native errors and transport failures | Preserve typed stale-generation, claimant, lease, authorization, idempotency conflict and uncertain-outcome failures; one request, no fallback, replay, generated key or substituted generation. |
| Legacy /handoffs, claim/release/complete/return/withdraw/retarget; /epics, /board, /history and /verify/write | Release-owned bridge/helper remain explicitly unavailable, exit 2, empty stdout, before profile/key/network. Legacy complete's empty object is not a native return or acceptance. These positive facade mappings remain owed. |

The client cannot grant authority or decide current ownership/lease/generation. The existing server state machine, scoped transaction, audit/outbox and receipt are authoritative. No server/model/SQL, facade route, credential, live script or live data change in this slice.

## Execution and evidence

1. Commit this plan in the source worktree at /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r98-coordination-write-contract-20261005/docs/plans/r98-coordination-write-contract.md before tests or source changes.
2. Freeze /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r98-coordination-write-contract-20261005/tests/test_agent_coordination_write_contract.py. Capture its test-first RED commit, command, stdout, exit and SHA-256. Expected failures: malformed native writes access the reader/transport, and malformed 2xx receipts are accepted. Existing unavailable behavior is a preserved GREEN baseline, not claimed RED.
3. Change only /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r98-coordination-write-contract-20261005/src/cortex_v2/clients/client.py, retaining the frozen tests. Use the registry's models; no parallel business logic or state cache. Quantifiable target: all frozen checks pass with zero reader/transport calls for local malformed input; exactly one for valid or remotely refused writes.
4. Run full client/CLI/MCP/agent consumer source suites and coordination models/state/eligibility/operation suites, no -k filters. Literal receipts under /Users/amadmalik/DevVault/helix/output/Cortex/local-deployment-r63/2026-10-05/coordination-write/. SSD cache/TMPDIR/basetemp. Loopback HTTP proves transport and preserved wire data; no actual credential store or issued key. Existing DB integration suites require a separately admitted disposable DB and do not become runtime evidence here.
5. Fresh-context final verifier; freeze, push draft PR, return to Kai, update owned TODO. Re-read inbox before mutation/freeze/return. Follow the r100 inbox wait after the return, checking its stamp every 30 seconds for up to 30 minutes.

Vera source review, integration, hosted CI, new native package, released/installed full caller qualification, converted-data rehearsal, live switch and rollback keep their own gates. This slice qualifies neither a positive legacy coordination facade nor database atomicity/concurrency/runtime. Search/ingest and dynamic mappings remain later contracts.

Author compatibility amendment: freeze /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r98-coordination-write-contract-20261005/tests/test_coordination_receipt_compatibility.py. The native repository returns policy_revision 0 when no writer policy exists; preserve that valid receipt, while retaining strict integers and safe malformed-envelope refusal. Initial 1 failed / 6 passed captured before the one-line correction. Author verifier also requires preserving an available body request_id on uncertain receipts, consistently with the existing native client metadata rule; freeze a separate metadata test before repair. Receipt validation is structural: exact operation and handoff ID, committed state and native field types. The client does not reproduce the server transition machine or decide current ownership/lease/generation. The generic success fixtures do not qualify native handler outcomes or DB atomicity.

Verifier key amendment: an all-space key, or leading/trailing spaces, is not preserved by HTTP field parsing. Freeze local pre-key refusal tests for these values and allow valid internal spaces unchanged. The source repair rejects surrounding whitespace rather than trimming or replacing a caller key.
