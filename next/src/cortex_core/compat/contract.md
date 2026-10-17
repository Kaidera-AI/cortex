# Option-A command compatibility contract

Normative spec only, H-D614, 17 Oct 2026. Owner nemo; Finn implements daily
translation; Cox owns canonical API/records. Accepted INDEX v2 J05/J09/J10 wins
over earlier proposed taxonomy. No compatibility runtime is introduced.

## Authority and profile

A requires explicit `ai_mode: off`; missing mode refuses configuration admission.
Do not construct a provider, discover a model, read a provider key, or attempt
provider DNS/connect. A has no provider/model/endpoint/prefix/dimension default.
Provider settings/client/qualification are .005; handoff dispatch .006;
historical context/team move .007+. Logging event_type=handoff is a memory note.

Bearer and X-Project use existing scoped admission. Writes require X-Agent-Name
equal to the enrolled principal; reject duplicate auth/project/writer headers.
Headers cannot create membership. Authenticate/authorize before capability
refusal; authorized credential reads are allowed, product effects are not.
An unavailable API never falls through to v1. No request-time enrollment.

## Selected field and wire map

The JSON below is the normative *specification* field map consumed by its own
assertions, not runtime configuration, a route registry, or a second OpenAPI.
Cox's ONE bound registry owns actual published methods/schemas/errors/links;
Mike's generated test must cover every published operation, beyond this subset.

| Input | Canonical meaning / output |
|---|---|
| boot AGENT | GET /boot/{agent}; enrolled identity only, boot + persona with schema_version/project/agent/agent_identity/role/metadata.feature_profile=keyword-memory. Identity label must match enrollment. Budget bounds rendering. Full/query historical context refuses, no fabricated arrays. |
| log event_type,summary | section=event_type, content=summary; preserve original fields in the SAME memory-kind payload. |
| category,importance,files_affected,metadata | Omitted defaults null/5/null/null; explicit null preserved. No coercion. Category string, importance integer, files array of strings, metadata JSON object; each optional field also accepts null. No undocumented importance range. |
| server marker/writer/source | Server alone sets record_class=log and authorized writer; source=log:<canonical Idempotency-Key UUID>. Clients cannot supply these fields. |
| /memory/write client alias | Translate to POST /memory using existing section/content/category/source validation. No /memory/write server writer/route; ordinary memory's category requirements/default stay unchanged. Reject normal-memory log: source creation/overwrite. |
| GET /records/{id} | Authorized canonical id/kind/revision/payload_sha256 plus memory section/content/category/source; for logs expose preserved event_type/summary/files_affected/importance/metadata and server marker/writer too. No legacy ID-equivalence promise. |
| GET /verify/write | kind=decision/lesson/team_event + id reads that SAME record; scope, log marker and stored event type must match. decision/lesson require matching event_type; team_event allows the ten log types. Wire row retains id/project/agent_name/event_type/summary/files/category/importance/metadata; files aliases files_affected, agent_name aliases authorized writer. No extra table/row/team_event_id. |

Required event_type and summary must be strings; event_type is one of the ten
listed below. Summary may be empty; never strip, prepend GOAL_ID, normalize
Unicode or convert CRLF/LF. Preserve decoded string UTF-8 bytes, including quotes,
non-ASCII and trailing newlines; equivalent JSON escaping is not a raw-body-byte
promise. Files preserve order/duplicates/strings; metadata preserves finite JSON
values structurally (object order is not semantic). Empty string/list/object and
null remain distinct. Reject invalid Unicode, duplicate keys at any depth,
non-finite JSON, unknown keys and legacy supersession fields. Log body is bounded
at 256 KiB UTF-8 within Core's outer bound; canonical text projection
section + LF + content is at most 16 KiB UTF-8. Reject overflow before mutation.

## One transaction, replay and verification

Use existing Records memory transaction and C11b source-scoped UUIDv5 identity:
UUIDv5(NAMESPACE_URL, "cortex.memory:<tenant UUID>:<project UUID>:log:<key>").
Idempotency-Key is a required canonical lowercase hyphenated UUID, chosen once
by the CLI/operator and reused after lost acknowledgement. Preserve existing
principal-scoped request locking/replay. Normalize only omitted optional fields
to the explicit defaults before intent hashing; include every original field,
server writer and exact strings. Same key/intent returns the committed receipt;
changed intent or cross-writer source collision is 409 and cannot overwrite.

ONE canonical transaction commits payload, revision, idempotency receipt and the
existing c06 event. Require coordination.c06_record_event to return a non-null
event_id; absence aborts, never eventless success. Do not use C07 Jobs or another
writer/outbox. Log creation starts at expected_revision=0; replay creates no new
revision/event. Canonical delete remains the authorized Records tombstone path.

Success after commit carries logged=true,id,event_type,verified=true,
embedded=false,revision,event_id,payload_sha256. verified=true additionally
requires authorized post-commit read and exact field/digest comparison.
A lost commit acknowledgement or verification refusal is uncertain, not proof
of no write: retain key/available authorized receipt, propagate the actual typed
error/nonzero exit, never mint a retry key or fabricate verified=true.

## Keyword admission and errors

CLI explicitly sends mode=keyword,type=all,rerank=false,graph=false,hall=project;
missing mode is 400 invalid_input, not a default. q is 1–512 characters and limit
1–100. Non-all known taxonomy selectors and semantic/rerank/graph requests return
the authenticated 503 unshipped packet, never ignored flags or keyword downgrade.
Other malformed inputs are 400. Existing authorized scan <=100 records retains
canonical freshness, partial/scan_limit/unsupported_text and typed 503; never
report incomplete data as complete. type=all includes memory and marked logs.

Shared error envelope: error{code,capability?,state?,reason?,retryable}.
400 invalid input (including verify kind/type/marker mismatch); missing/invalid
credential 401; forbidden 403; absent/private record 404; CAS/intent collision
409; tombstone 410; actual unavailable/deadline/incomplete reasons remain 503.
Authenticated valid unshipped requests use the exact packet below, before
provider construction, SQL mutation/event/job/file writes or external I/O.
A runtime error must retain its real reason; it is not an unshipped-operation
success substitute. CLI preserves packet and nonzero exit. Core enforces these
guards even when the CLI is bypassed.

## Normative field map

```json
{
  "contract_version": "cortex.compat.option-a.v1",
  "ownership": {
    "spec": "nemo",
    "daily_adapter": "finn",
    "canonical_api_records": "cox",
    "runtime_reads_this_map": false
  },
  "profile": {
    "ai_mode": "off",
    "missing_mode": "refuse",
    "provider_construction": false,
    "provider_attempts": 0
  },
  "mapping": {
    "boot": {
      "method": "GET",
      "path": "/boot/{agent}",
      "auth": true
    },
    "log": {
      "method": "POST",
      "path": "/log",
      "auth": true
    },
    "memory": {
      "method": "POST",
      "path": "/memory",
      "auth": true,
      "client_alias": "/memory/write",
      "server_alias": false
    },
    "record": {
      "method": "GET",
      "path": "/records/{id}",
      "auth": true
    },
    "verify": {
      "method": "GET",
      "path": "/verify/write",
      "auth": true
    },
    "search": {
      "method": "GET",
      "path": "/search",
      "auth": true
    }
  },
  "fields": {
    "event_type": {
      "required": true,
      "type": "enum",
      "values": [
        "commit",
        "decision",
        "lesson",
        "started",
        "stopped",
        "blocked",
        "unblocked",
        "bug",
        "handoff",
        "question"
      ],
      "payload": [
        "section",
        "event_type"
      ]
    },
    "summary": {
      "required": true,
      "type": "string",
      "payload": [
        "content",
        "summary"
      ]
    },
    "category": {
      "required": false,
      "type": "string|null",
      "omitted": null,
      "payload": [
        "category"
      ]
    },
    "importance": {
      "required": false,
      "type": "integer|null",
      "omitted": 5,
      "payload": [
        "importance"
      ]
    },
    "files_affected": {
      "required": false,
      "type": "array[string]|null",
      "omitted": null,
      "payload": [
        "files_affected"
      ]
    },
    "metadata": {
      "required": false,
      "type": "object|null",
      "omitted": null,
      "payload": [
        "metadata"
      ]
    }
  },
  "validation": {
    "unknown_or_supersession_fields": "reject",
    "integer_booleans": "reject",
    "utf8": "exact decoded strings; no trim, normalization or prefix",
    "invalid_unicode": "reject",
    "duplicate_keys_or_nonfinite_json": "reject",
    "null_empty_distinct": true,
    "files_order_duplicates": "preserve",
    "log_json_max_utf8_bytes": 262144,
    "projection_max_utf8_bytes": 16384,
    "projection": "section + LF + content"
  },
  "log_write": {
    "kind": "memory",
    "transactions": 1,
    "event_guard": "coordination.c06_record_event must return non-null",
    "c07_jobs": false,
    "server_fields": {
      "record_class": "log",
      "writer": "authorized enrolled principal.name"
    },
    "normal_memory_log_source": "reject"
  },
  "replay": {
    "key": "required canonical UUID Idempotency-Key",
    "source": "log:<key>",
    "record_id": "C11b source-scoped UUIDv5",
    "intent": "normalized optional defaults + all fields + authorized writer; exact strings",
    "same_key_same_intent": "same committed receipt; no new revision/event",
    "same_key_changed_intent": 409,
    "foreign_writer_collision": 409,
    "lost_ack": "uncertain; preserve key; never generate retry key"
  },
  "verify": {
    "aliases": {
      "decision": [
        "decision"
      ],
      "lesson": [
        "lesson"
      ],
      "team_event": [
        "commit",
        "decision",
        "lesson",
        "started",
        "stopped",
        "blocked",
        "unblocked",
        "bug",
        "handoff",
        "question"
      ]
    },
    "log_marker_required": true,
    "stored_type_check": true,
    "mismatch": 400,
    "success": "authorized post-commit read matches exact fields and digest",
    "fabricated_team_event_id": false
  },
  "search": {
    "explicit": {
      "mode": "keyword",
      "type": "all",
      "rerank": false,
      "graph": false,
      "hall": "project"
    },
    "taxonomy": {
      "knowledge": 503,
      "decisions": 503,
      "lessons": 503,
      "events": 503
    },
    "missing_mode": 400,
    "scan_max_records": 100,
    "incomplete": "503; preserve partial/scan_limit/unsupported_text and freshness"
  },
  "unshipped": {
    "response": {
      "status": 503,
      "code": "capability_unavailable",
      "reason": "unshipped_operation",
      "retryable": false
    },
    "authenticate_authorize_first": true,
    "before_effects": true,
    "effects": {
      "provider_construction": 0,
      "provider_dns_connect": 0,
      "sql_mutations": 0,
      "events": 0,
      "jobs": 0,
      "files": 0
    },
    "provider_defaults": [],
    "families": [
      "semantic",
      "rerank",
      "judge",
      "provider_settings",
      "taxonomy",
      "graph",
      "handoff_dispatch",
      "full_boot",
      "history"
    ]
  },
  "coverage": {
    "scope": "selected-command-spec-only",
    "assertions": [
      "authorized_expected_outcome",
      "status",
      "content_type",
      "request_schema",
      "response_schema",
      "auth_scope",
      "links"
    ],
    "live_gate": {
      "every_published_operation": true,
      "requests_min": 1,
      "required_skips": 0,
      "all_401_accepted": false,
      "source": "Cox bound registry/OpenAPI"
    }
  }
}
```

## Fail-capable acceptance and exclusions

The module tests assert this SPEC only: unshipped_provider_refusal checks the
exact refusal/zero-effect requirement; generated_coverage forms every selected
mapping × assertion obligation and rejects missing coverage/all-401 credit.
These are not observed HTTP requests, runtime/provider tests, or release passes.
Mike/Cox must bind the real generated export and run every published authorized
outcome with status/content-type/schema/auth/links and zero required skips;
missing link/path/schema, unknown route/method, all-401 or empty requests fails.
Unshipped families must produce the exact packet with zero observed effects.

Downstream causal cases: UTF-8/nullable fields exact write-read-restart; same key
replay vs changed byte 409; reserved-source overwrite; wrong verify kind/marker;
101st scan/unsupported text; foreign/read-only/revoked identities; direct HTTP
unshipped bypass; missing mode/non-all taxonomy; observer positive control then
zero provider DNS/connect. Paired installed proof is Mike's ONE native Rocky
x86_64 + Mac arm64 gate on the same signed build, never this document suite.

Source reuse and licence are recorded in UPSTREAM.md. Runtime implementation,
old migration edits, v1 service/DB/PATH, provider/settings work, local AI, merge,
signing and release are outside this slice. Hale reviews the exact pushed head;
Cox owns ordered composition and reruns. No author release/merge approval.
