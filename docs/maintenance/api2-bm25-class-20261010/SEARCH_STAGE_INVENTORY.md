# execute_search invalidation inventory

Source base: `db0989d25930cfb70d662f9cffade79673ce446e`; candidate changes only the two BM25 predicate lines, so the line numbers below are unchanged. This is the inventory requested by Kai at 23:38, not a runtime acceptance claim or permission to repair additional stages.

| Stage / table | Excludes invalidated? | Exact source evidence |
|---|---|---|
| Exact-ID / decisions | **No** | `packages/api/main.py:6567` includes decisions; the first query at 6571 has only `id::text ILIKE $1`, then the content query at 6576 has only `id::text = $1`. Neither includes invalidation filtering. |
| Exact-ID / lessons | **No** | Same shared loop and two queries at 6567, 6571 and 6576. |
| Exact-ID / handoffs | **No** | Same shared loop and queries. The handler elsewhere explicitly knows handoffs.invalidated_at (`main.py:18766`), but this lookup does not filter it. |
| Exact-ID / work_products | **No** | Same shared loop and queries; neither `invalidated_at IS NULL` nor `status = 'current'` is required. |
| Work-product lexical/current brief | **Yes** | `execute_search` invokes `search_work_products` at 6615; that helper passes status current at 6389 to `fetch_work_product_briefs`. Its SQL at 6147–6150 requires `wp.invalidated_at IS NULL` and the requested status. |
| Trigram / decisions | **Yes**, in the called SQL bridge | `main.py:6947–6953` calls `cortex_search_decisions_trigram`; `packages/schema/migrations/2026-08-31-03-repair-decisions-trigram-security-definer.sql:35` requires `d.invalidated_at IS NULL`. This is the source definition, not verification of a deployed database function. |
| Trigram / lessons | **Yes** | `main.py:6924` sets `invalidation = 'AND invalidated_at IS NULL'`; the non-decisions candidate query interpolates it at 6967, before its candidate limit. |
| Vector / decisions and lessons | **Yes** | Both are in the table map at 6447–6454; the non-knowledge branch at 7160 supplies `AND invalidated_at IS NULL`, interpolated into the query at 7175 before ORDER BY/LIMIT. |
| Vector / work_products | **Yes** | Dedicated query at 7037–7041 requires project, status current, `invalidated_at IS NULL` and an existing embedding. |

Coverage: handoffs is read only by the exact-ID path in this function; work_products is read by exact-ID, its lexical helper, and its dedicated vector stage. The general trigram/vector table map contains decisions, lessons and knowledge, not handoffs/work_products. The graph helper at `main.py:4289–4363` reads cortex_entities/cortex_relationships, not these four source tables. Artifact, knowledge, captured-pattern and message stages do not read the four inventoried tables. Rerank/dedup consume prior candidates without querying these tables or adding an invalidation filter.

## Named follow-up, not repaired here

**API2-EXACT-ID-INVALIDATION**: the shared UUID/prefix path can select retained invalidated decisions, lessons, handoffs and work_products, and returns early at 6595–6604. Require a separate Kai disposition and focused RED proof for each table; keep any intentional historical lookup policy explicit. The work-product case also lacks its other stages' current-status contract. No exact-ID source was changed in this PR.

## Source custody

The full inspected source is preserved on the SSD in `output/Cortex/v004/api2-bm25-class-20261010/inventory-source/`. The full paths and SHA-256 values are in `inventory-source.json`: main.py SHA-256 `e17193a81bf3ac112bdb8594df04ba321ba63c7349af2d565d85c012b4f7df17`; trigram migration SHA-256 `3d054a115b18bd793018a8fd29d8df16ea3f28ac234378d94ab668a2a9927278`.
