# Run-profile storage A (H-D641 / H-D643)

The sole desired-profile authority is `agents.capabilities.run_profile` in Cortex
v1. The maintained source is `packages/api`; `.agents/api` is the deployed Helix
projection. This candidate changes source only. Kai/CTO own projection/deployment
and the coordinated KOS cutover. No migration, running service or live data changed.

## API

All requests require the existing project/auth boundary. The immutable worker key
is the existing canonical `name@project`; a label is never a lookup or transport
identity. `capabilities.display_name` is the mutable display label, defaulting to
the canonical name for presentation only. Role/team/provider/harness choices stay
data; this change creates no UUID roster or fixed worker/provider/model lists.

- `GET /agents/{canonical-name}/run-profile`: desired profile; 409 `unconfigured`
  includes current revision, missing fields and partial profile. No registry-model,
  environment, app-DB or vendor fallback. Corrupt stored data refuses `invalid_profile`.
- `PUT /agents/{canonical-name}/run-profile`: owner-authenticated partial update,
  `{expected_revision: 0, fields: {...}, display_label?: "..."}`. Initial revision
  is zero. Stale revision returns 409 `conflict`, including the current revision.
  A label-only update uses the same CAS. Absent fields remain byte-equivalent.
- `GET /agents/{canonical-name}/run-profile/history?after_revision=0&limit=100`:
  ascending revision history from existing project-scoped `team_events` (limit ≤500).
- `/roster` and project runtime include desired profiles and labels alongside
  existing fields. Legacy `agents.model` is presentation/compatibility data, never
  a source for the new profile API.

Stored shape (all nine fields are explicit; `fast:false` and `router_mode:off`
remain values):

```json
{"schema_version":1,"revision":1,"fields":{
  "harness":{"mode":"set","value":"codex","set_by":{"actor":"local-owner","source":"user"},"version":1},
  "provider":{"mode":"set","value":"selected-provider","set_by":{"actor":"local-owner","source":"user"},"version":1},
  "model":{"mode":"set","value":"selected-model","set_by":{"actor":"local-owner","source":"user"},"version":1},
  "effort":{"mode":"set","value":"high","set_by":{"actor":"local-owner","source":"user"},"version":1},
  "fast":{"mode":"set","value":false,"set_by":{"actor":"local-owner","source":"user"},"version":1},
  "permission":{"mode":"set","value":"workspace","set_by":{"actor":"local-owner","source":"user"},"version":1},
  "autonomy":{"mode":"set","value":"interactive","set_by":{"actor":"local-owner","source":"user"},"version":1},
  "router_mode":{"mode":"set","value":"off","set_by":{"actor":"local-owner","source":"user"},"version":1},
  "allowed":{"mode":"set","value":[],"set_by":{"actor":"local-owner","source":"user"},"version":1}}}
```

PUT fields contain only `{mode,value}`; clients cannot set provenance/version.
Each explicitly written field increments its own version; untouched fields keep
both version and provenance. Server attribution is `local-owner` for the legacy
shared admin credential, or the authenticated service identity for an admitted
`instance:admin` credential. `X-Agent-Name` never supplies profile attribution.
A credential able to register ordinary workers cannot raise profile permissions.
`set_by.source` reserves `project_default`/`router` for future admitted writers;
this endpoint only attributes `user`. Auto/inherit have null values, not pins.
Router choice of permission/autonomy/router/allowlist is refused. An allowed tuple
contains exactly harness/provider/model/effort/fast/permission/autonomy (≤32 entries).

A row lock serializes CAS, capability replacement and the audit append in one
transaction. Failure of any part produces no successful acknowledgement or partial
write. Audit contains before/after snapshots, immutable worker key and revision.
Ordinary `/log` and local-sync event inputs cannot forge its reserved event type.
Registration rejects protected profile/label keys; existing profile keys survive
unrelated registration updates. Vault import validates profile shape, preserves
historical provenance, and its existing insert-only conflict behavior cannot update
an existing worker profile. Full project export/restore already carries agents,
capabilities, events and project references: no new export format or profile mirror.

## One-writer cutover and the consumer boundary

**Chosen path: retire KOS `agent_settings` as a run-profile authority**, rather
than maintain a read-through cache. Preserve its unrelated settings and frozen
legacy profile data as migration input. This Cortex source PR does not rewrite
KOS's frozen .016 store or enable its dispatch. The resolver/cutover integration
must bind every managed/manual launch and profile edit to this API before enabling.

Freeze legacy profile writers; compare legacy values with the desired Cortex
profile and report conflicts instead of picking a winner. An authorized operator
uses this same CAS endpoint to configure reconciled profiles, verifies project
export/restore, then retires legacy profile reads/writes in the coordinated KOS
candidate. Do not dual-write, copy back on failure, resume JSON fallback, delete
unreconciled data or enable Cortex-A against active legacy profile writers.

Pike's resolver owns catalogue/entitlement/current authorization, exact selected
provider refusal, final revision revalidation, native permission effects, admission,
isolated config and requested/applied/observed receipts. A successful storage read
is desired data, never execution permission or native qualification. Unsupported,
revoked, missing or unresolved selections must refuse before claim/spawn/provider
call; auto/inherit need an admitted resolver/default, not an invented model. Pilot
uses fully explicit fixed fields, router off, fast false. DFP-03 native effects and
the full rename/claim/run/outbox boot integration remain pilot-enablement gates.

## Verification on this Mac

From the task worktree, with the locked API dependencies and pytest installed:

```sh
PYTHONPATH=packages/api CORTEX_ADMIN_TOKEN=fixture-unused CORTEX_API_URL=http://127.0.0.1:1 CORTEX_PROFILE_FIXTURE_ROOT=/Users/amadmalik/DevVault/helix/tmp .venv/bin/python -m pytest packages/api/tests/test_run_profile.py packages/api/tests/test_run_profile_postgres.py -q
```

The PostgreSQL tests create their own unix-only cluster, refuse a live DSN, use
invented identities and remove only their owned fixture. They prove concurrent
CAS, rollback and vault restore with synthetic in-flight references; they do not
claim installed KOS/native provider qualification. CI runs the same source on Linux.
