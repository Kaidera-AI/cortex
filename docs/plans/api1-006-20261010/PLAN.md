# API1-006 UUID metric cardinality — next v0.1.004

Owner Bob, Kai item9. Separate branch/PR from fresh main, no rebase. Only prometheus_middleware in main.py, own tests and plan.

After call_next resolves routing, use the server-owned route.path template as endpoint label. For unmatched/early-refused/no-route responses, use one fixed unmatched bucket. Never use a raw request path as the fallback. Keep method label, duration measurement, response and /metrics exclusion unchanged. This also naturally collapses named-identity route parameters without guessing regexes.

Frozen Mike12:24 test_metrics_paths_collapse_different_uuid_instances, actual function AST binding and every assertion unchanged: RED before source, GREEN after. Add local in-process ASGI controls proving matched /handoffs/{id} template (not all labels flattened), static /health distinction, unmatched path grouping, and /metrics exclusion. No full product import/startup/host/DB/network. Original raw-path mutant must fail frozen cardinality and matched-template controls. Fresh memoryfree>=35% before tests. Independent exact-SHA review before merge, no installedv0.1.003 or security scope changes. API method-label cardinality and broader metrics policy remain separate.

Kai15:16 plan accepted. Added in-process synthetic auth401 early-refusal control; fixed unmatched bucket, never raw UUID path. No mounted/nested routers in current API projection; full server-owned route template retained.
