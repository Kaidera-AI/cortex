# Item 10 / API1-007 — graph edge direction PLAN ONLY

Owner Bob; next Cortexv0.1.004. No graph source or schema change has been made. Independent plan acceptance precedes implementation. PR64 has been marked ready per Kai15:20.

## Problem and minimal proposed change

Current search_graph unions outgoing and incoming relationships. Both projections call the search seed `seed_name` and the other entity `related_name`, then always format seed -> related. For an incoming edge, the seed is actually the target. Reverse only the incoming SQL projection's endpoint values: `src.name/entity_type AS seed_name/seed_type`, `s.name/entity_type AS related_name/related_type`. The outgoing projection and formatter stay unchanged; the existing internal column names then represent actual source and target in both arms. Preserve filters, parameters, limits, relation type, description, output shape and counts. No API/schema/graph-engine change.

## Frozen row-double compatibility check

Archive and hash Mike's original12:24 `test_incoming_graph_preserves_actual_direction` method and row double. Preserve every self.assert and expected text verbatim. Its hardcoded incoming row describes the OLD projection (seed=Target, related=Source); simply swapping those fixture values after the fix would be circular.

Instead, amend ONLY Connection.fetch's transport: keep `self.sql`, delegate the actual source SQL and parameters to the owned disposable PostgreSQL query port, and return its actual rows. No handcrafted after-fix row values. Prove the selected record keys/types equal the original double's six-column contract before and after: seed_name/seed_type/related_name/related_type/relationship_type/rel_description. The original assertion still requires Source(service) --depends_on--> Target(service), and its incoming-join SQL assertion stays unchanged. This protocol amendment is part of the plan to accept explicitly. An unexpected KeyError/type/fixture failure never counts as RED/GREEN or a mutant kill.

## Actual SQL proof / RED-first sequence

1. Fresh origin/main or later owned branch; forward merge only, no rebase. Fresh memoryfree>=35% before starting labelled kaidera-dev-graph-proof-1, networknone/no published ports, max1GiB/2CPU, pinned already-present upstream pgvectorPG18. Validate ownership label and no routing to live5500/8501 before SQL. Cleanup in finally, retain absence receipt. If resource/schema gate fails, stop and report; no guessed replacement schema.
2. Bootstrap authoritative owned cortex-schema-full.sql/migration inputs. Register artificial project and add only synthetic Source/Target service entities and directed Source depends_on Target, plus unrelated cross-project control. No production dump/real rows.
3. Execute the ACTUAL search_graph function body via source-bound AST with the query port. The port executes its exact SQL using PostgreSQL PREPARE with the original parameter order; transport can use podman-exec psql CSV-to-record conversion, so no host listener/DSN is needed. No SQL-direction guessing or replacement implementation.
4. Frozen incoming-direction assertion must go RED on the original producer; real outgoing control must already pass. Add incoming/outgoing, description, empty-result, project/room filtering and repeated-call controls. Record actual raw row shape/projection alongside output text. Commit tests BEFORE product source.
5. Make only the incoming projection swap, then GREEN against unchanged assertions and actual SQL. Mutant restores original incoming projection and must fail the SAME incoming text assertion at test body. Outgoing control remains green. Do not change row fixtures/assertions after fixing source.
6. Small PR in Kaidera-AI/cortex (always explicit-R), source/test/accepted plan only. Vera exact-SHA review and Kai adjudication before merge. Native whole-product/search-budget/runtime/release/install remain separate gates. No liveAPI, security remediation, credentials, v0.1.003 or container image build.

## Receipt contract

Return `2026-10-10_bob_kai_fix-api1-007.md`: exact origin SHA, frozen assertion and double binding, owned-schema hash, actualSQL/parameter/row receipts, literal RED/GREEN/mutant outputs, resource admission/cleanup and limitations. If not reproduced, NOT-REPRODUCED with the test. This document is a future plan; no graph tests or stack have run yet.

Kai15:24 plan accepted with JSON transport required. Native query is wrapped SELECT row_to_json(t) FROM (<exact SQL>) t and PREPARE preserves original parameter order/types. NULL-description row control added. Freshbranchbase91a72d0; no graph schema/API change.
