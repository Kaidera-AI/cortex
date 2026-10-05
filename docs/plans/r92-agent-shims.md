# R92 first agent shim: project reads from release bytes

Owner: ren-cx@helix. Authority: /Users/amadmalik/DevVault/helix/docs/handoffs/2026-10-05_kai_rulings-r92.md and the accepted L2 small-reader implementation in /Users/amadmalik/DevVault/helix/Program/Cortex/local-deployment/PLAN_2026-10-03.md section 4. This is a bounded implementation of that admitted source work, not package admission.

## Pre-edit evidence and scope

Current and outcome project: helix; Cortex-owned agent transport/release source only. Live boot verified ren-cx@helix, cortex-chief. Kai remains accountable return owner; Vera reviews the frozen source. Gavel/Jev are held by Kai's standing ruling. No external decision input is sent.

Clean owned worktree: /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r92-agent-shims-20261005, branch ren-cx/r92-agent-shims-20261005, base and freshly verified origin 0f80eca9f71f1fe324a36034492c9b431288061f. SSD UUID 8071CB26-B817-496E-A1FC-A8FE9A87C184. Host memory 51 percent free before edits. Public scratch/tests only on SSD; no actual credentials or keychain operations.

The current native host build freezes only the installer. The current /Users/amadmalik/DevVault/helix/.agents/scripts/cortex-projects invokes GET /projects and formats the legacy response with Python. The canonical live helper still uses ctx1 credentials. No live helper is edited. This slice creates the Cortex release's native agent request program and its first project command; remaining callers stay explicitly unqualified.

## Contract

Select an explicit v2 member through load_member_profile using its non-secret connection profile and physical project root. Keep no startup bearer. For GET /projects, read headers exactly once immediately before the existing selected-origin transport. Keep the bearer inside that process. Refuse missing/locked/expired local keys safely, without lookup of another identity. Remote 401/403/409 and redirects are failed requests, never retries or alternate destinations. Preserve expiry in the in-process response; raw API mode retains the legacy JSON body rather than applying a native data envelope.

Initially admit only GET /projects with no query/body and an optional agent name matching the selected member. Unsupported routes, methods, identity overrides, arbitrary curl options and privileged calls refuse before private access. This is an explicit incomplete facade ledger, not removal of the other callers' obligations. Their contracts are added as separate RED-first slices before L4.

The native projects command prints today's table byte for byte for valid legacy data. Missing or malformed project arrays/fields are errors; they never become '(no registered projects)'. The valid explicit empty array retains that message. Validate the whole response before emitting a table. Freeze the actual old command as a public fixture and compare its complete stdout against the new command.

## Files and sequence

All source paths below begin /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r92-agent-shims-20261005/.

1. Commit this plan to docs/plans/r92-agent-shims.md. Freeze tests/test_agent_project_shim.py and tests/fixtures/legacy-cortex-projects (exact old command bytes); execute expected behavioral RED before implementation.
2. Add src/cortex_v2/cli/agent_request.py: bounded project request, safe CLI errors and native table presentation. Add scripts/release/agent_request.py as its freeze entrypoint.
3. Add scripts/agent-shims/cortex-projects and scripts/agent-shims/_cortex_api.sh. Both invoke the sibling native cortex-agent binary; the helper only admits the mapped read, sends no token into shell and preserves the raw-body/exit contract. Profile selection is CORTEX_CONNECTION_PROFILE, a non-secret file path. Native projects needs no target Python.
4. Update scripts/release/build-candidate.py: freeze cortex-agent using the same pinned native builder, require arm64/help proof, inventory its archive and ship the two shims beside it. Assembly restores executable modes lost by GitHub artifact transfer. Bind new bytes in the existing whole-package manifest/checksums; no existing release asset is replaced.
5. Run the unchanged contracts with the member/client/MCP/transport regressions. Author verifier checks the exact frozen delta. Push a draft review branch and return its receipts to Kai for Vera. Native hosted build, Mac ACL enrollment, actual converted data, released caller qualification and new package admission remain gates.

## Proof and risks

Ephemeral loopback receivers and in-memory unissued reader markers prove replacement between requests, local failure after success, one call/no replay, explicit scope, body/error preservation, credential-free stdout/stderr and no ambient proxy. Captured old command executes only against a synthetic helper returning the same public project response, with no credential access. Shell helper tests use a disposable sibling runner bound to the real CLI module and fake reader. Builder wiring tests execute the host-freeze helper with a recording builder, proving commands and inventory flow without claiming native binary execution.

The source depends on three still-unaccepted review slices (transport, member client, MCP). Their applicable ACCEPTs and a new named admission precede any new package. This turn never installs, publishes, merges, issues keys, modifies live scripts, starts containers or touches live Cortex. Re-read /Users/amadmalik/DevVault/helix/docs/handoffs/INBOX_ren-cx.md at freeze/return. Stop dependent work on scope drift, a memory reading below about 35 percent, secret leakage or unknown caller semantics; continue independent admitted source work.

## Captured caller proof amendment

Add /Volumes/WD-B-4TB/DevVault/helix/worktrees/ren-cx-r92-agent-shims-20261005/tests/test_agent_frozen_caller.py. Execute the actual captured command and new project shim with the real source bridge and ephemeral loopback server, using only an in-memory unissued reader. This proves source execution and proxy refusal; it is not native/released/installed caller qualification. The captured failure path exposed the helper's missing CORTEX_API display label: preserve that variable as the literal 'selected Cortex v2 origin', never copy an unvalidated URL environment into error text. Freeze the failing denial test, then add the label without changing assertions. Final runs set explicit SSD --basetemp and TMPDIR; initial source fixture runs used the OS temporary directory and contained public fixture bytes only.
