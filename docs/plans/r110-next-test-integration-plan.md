# R110 proposal: integrate accepted Cortex slices into the next unsigned TEST

Owner: ren-cx@helix. Authority: /Users/amadmalik/DevVault/helix/docs/handoffs/2026-10-05_kai_rulings-r110.md. Plan only; no integration or package action is admitted now.

1. Refresh exact source review returns and live origin/PR heads for Cortex #4 through #11, including parallel #8, and the dynamic caller PR if Kai adds it to this candidate. PR #11 is currently queued for review. Freeze a table of exact base/head SHAs, dispositions and scope. All applicable ACCEPTs must precede the integration; changed reviewed bytes return for review.
2. Create a clean locked SSD integration branch from the accepted package base 99d792d5e6e564f1917458000263174ca29bd324 after refreshing it. Carry the accepted stack through the final accepted tip, then merge #8's roots-only delta explicitly. No silent omission of #8 or duplicate cherry-pick of ancestral slices. If conflicts arise, record them, obtain source review for any hand edit and freeze a new merge-only receipt.
3. Produce tree-diff evidence showing the integrated tree equals the accepted stack plus the exact parallel roots delta, with no other paths. A fresh author verifier checks tree equality and parents; Vera reviews the merge-only candidate on its exact SHA. Rerun complete consumer/source checks on the integrated SHA and freeze outputs without weakening failures.
4. Seek the named candidate-specific admission for one fresh first-attempt hosted CI run. Record exact source, workflow, run ID, attempt, every job and immutable native Mac/Linux image/host/package digests. Failed attempts remain visible; retry requires the current ruling. Source GREEN or an old CI run cannot qualify the new candidate.
5. New unsigned TEST publication requires the exact merge review ACCEPT, fresh CI end-to-end GREEN and named publication go. Use immutable versioned artifacts from that run. Download/read back size and digest, verify source/image labels and payload against Git, then perform the authorized literal guide install/restart smoke in a separately named root/port with pre/post unrelated-container inventory. Installer source and Mac key-reader behavior are included only at accepted bytes; actual released member/shim/full-caller qualification remains distinct.
6. After package acceptance, qualify the real frozen agent/Console/MCP callers from those released bytes on isolated converted data. Refreshed caller hashes and a complete route/method/query/body/scope/fence/status matrix are required. Positive legacy mapping obligations must be met before their callers qualify; explicit unavailable source checks do not satisfy them. Then the restored-copy conversion rehearsal, timing/zero-loss proof and switch/rollback sheet. Live backup, issuance, switch and retirement retain separate named CTO gates.

Permanent evidence will live under /Users/amadmalik/DevVault/helix/Program/Cortex/local-deployment/ and /Users/amadmalik/DevVault/helix/output/Cortex/. New temporary work uses /Volumes/WD-B-4TB/DevVault/helix/ after UUID preflight. Secrets remain off the SSD and out of receipts. Shared VM/live Cortex remain unchanged until a named action authorizes them. Dates become concrete in the candidate admission return after reviews; this plan makes no new release-date claim.

## Current PR head snapshot for planning

Fresh GitHub heads below are identifiers only; OPEN/draft state is not a review disposition. Refresh exact Vera returns and Kai rulings before integration. Snapshot receipt: /Users/amadmalik/DevVault/helix/output/Cortex/local-deployment-r63/2026-10-05/dynamic-callers/integration-pr-heads-plan-snapshot.json.

| Cortex PR | Exact head | Position |
|---|---|---|
| #4 | 04a77131a917caa315d61691ec75e2fc0ef3e32c | stack |
| #5 | 1a4cc4f360f27a7976e4544ac511dfd250f68fd9 | stack |
| #6 | 0f80eca9f71f1fe324a36034492c9b431288061f | stack |
| #7 | 562dfb2b644d708a358be732f9477b62ac1feeba | stack |
| #8 | 189f97357cef7db70415d628d5d9a87f114452ce | parallel roots delta |
| #9 | fe23fd18538b2a651ff4835437abbb75944cbc4e | stack |
| #10 | b40ca41a69373a3771a8cbb0a351e49bc5c62a84 | stack |
| #11 | b5926337d0198513e53f3e8fcc4d9e7dac4d3556 | stack |

Vera source ACCEPT for PR #10 is freshly recorded in /Users/amadmalik/DevVault/helix/docs/handoffs/2026-10-05_vera_kai_r106-coordination-write-review.md. PR #11 remains queued in r110. Earlier ACCEPTs and the package base must be revalidated before the candidate is frozen.
