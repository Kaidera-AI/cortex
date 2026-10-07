# R379 boot executable RED contract

Authority: accepted R2 BOOT-001 closure and Kai r379. R3 records the legacy precedence correction and ratified operational-envelope delta. This is tests/data only; source/schema implementation follows Vera RED admission.

Original nine packets are byte-identical copies of the r368-accepted capture. `raw-packet-sha256.json` binds them and the exact current cortex-boot script. The raw packets use skill_slug. The earlier capture manifest's derived slug summary is not an oracle. No original packet, timestamp or field is normalized away.

Expected native responses retain every original identity, role, rule, skill, handoff, harness, work-product/count/freshness value and the compatibility surface label. Only r379's two rendered-line replacements and native provenance/body-validation metadata are applied in independent golden patches. Existing-plane operational tail/context is a declared provider seam. No real SQL, authentication, imported skill body, installed path, key or live API qualification is claimed.

## Internal test boundaries

`cortex_v2.agent_boot` supplies:

- `select_boot_heads(rows, stream=agent|entry|publication)`: mapping from the exact logical key tuple to its highest enacted row, including an ineligible/retired head. It does not prefilter eligibility. Duplicate conflicting same-key revisions refuse rather than depend on row order.
- `resolve_boot_bindings(context, agent_rows=..., entry_rows=..., publication_rows=...)`: required own-agent binding plus eligible entry/publication heads. Input context is a trusted repository projection, not caller metadata. Returned keys are agent_binding, entry_bindings and publications. No permissions are derived from manifest strings.
- `project_boot_response(snapshot, budget=..., query=..., full=...)`: computes exact persona/core output and the r379 native envelope from selected rows. Snapshot contains context, the three revision streams, exact persona/skill/rule records and a declared operational provider result; it contains no expected response object. Context supplies registered project/actor/name/role, installation/owner and permanent code root.
- `load_boot_snapshot(connection, scope_context, ...)` is the declared repository seam for mounted-route tests. `read_boot(connection, scope_context, agent, budget=..., query=..., full=..., agent_label=...)` independently binds the URL/optional header to the credential-derived source context, resolves scoped reads only, and projects the result. Repository/RLS/native tests remain later gates.

`cortex_v2.cli.agent_boot.run` and agent_request's boot/raw-GET mapping use one fresh selected member reader per request. They validate the reply's own project/agent, registered permanent root, exact body-pin coverage/digests and physical regular/no-follow bodies before printing any output. No cache/decoy token, retry, extra scope grant or body materialization is permitted. The released wrapper forwards the original options and non-secret profile.

The body proof uses cortex.boot_validation.v1 exactly as the ratified consult: selected project/actor/binding revision/root/persona reference, canonical scope/entity/revision/body_ref/body_sha256 pins, and exact binding/publication references. Rule bodies remain inline; no new rule-file home is introduced. Public skill bytes and UUIDs are explicitly synthetic. Matching physical fixture files are created only in a unique main-disk OS temp root; no SSD deletion occurs.

## Coverage and effects

The five BOOT-001 families cover all three stream histories, ascending/descending/interleaved order, exact reactivation references, unrelated logical keys, head-before-actor/role/owner filtering, distinct owner publication after withdrawal and actual projection coupling. Scope/actor isolation remains explicit. Retirement never means erasure of prior disclosure.

The duplicate-slug cases assert project-before-global, then bound priority descending, then version descending. Agent and functional-role subjects affect eligibility only. The frozen local legacy countermodel's role pin2/priority99 beats agent pin1/priority1; raising the agent priority makes its pin1 win. No new specificity order is hidden in the oracle.

The independent old budget oracle contains the exact original truncate function and budget-loop AST, with the current50..500 clamp. Cases exercise P3/P2/P1 cut order, character-divided-by4 arithmetic, mandatory over-budget retention and the unchanged full flag. This is not context.prepare budget_bytes or the future8KB efficiency qualification.

Nine actual legacy command controls emit the original packets verbatim. Nine native CLI receiver cases verify original query construction, no request body/idempotency or extra read scope, current no-appended-LF output, one fresh reader and unrelated-cwd behavior. Named missing/changed/symlink body failures, wrong registry root, missing/malformed proof, foreign identity/project, duplicate pins/JSON keys, non-finite JSON and HTTP denial print no success and do not retry. Offline help is profile/key free. Unsafe raw method/origin/query options remain refused before a profile is loaded.

Mounted HTTP cases exercise the actual app route and actual boot composition, with declared repository/auth seams. The selected scope is readable, not writable/publishable; only transaction-local set_config scope setup may execute. No command receipt or source/data write is accepted for GET.

Optional unavailable operational projection reports availability.operational={state:unavailable,reason:projection_unavailable}, null counts and no invented work products/no-pending success. A missing required identity/manifest/body proof refuses explicitly.

## Freeze and admission

122 cases:108 missing-component RED assertions and14 already-green independent oracle/refusal controls. All previous source/tests/helpers, the11 ratified replacements and explicit native exclusions are retained. Complete selected/portable RED gates and zero-error collection run after a tests-only commit and immutable freeze. The source/dependency snapshots must remain identical during each gate. Any additional failure is unadmitted and preserved, never converted into a new skip.

This contract is not whole producer, DB/RLS/import, native body/key/runtime, merge/push, package/sign/install or release acceptance.
