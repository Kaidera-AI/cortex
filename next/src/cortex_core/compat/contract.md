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

## Day-2 owner conformance (H-D622)

The `day2` section freezes obligations against the exact published owner heads
and source hashes below. It records foundation behavior separately from proposed
joins and missing implementation. These assertions compare documents, not a
composed runtime. Source snapshots and independent extraction checks are in the
same-row evidence receipt; no owner source is copied into runtime or executed by
this module. A newer owner interface requires an explicit reconciliation receipt,
new exact source pins and conformance check; these pins grant no future acceptance.

Cox's actual bootstrap decoder requires explicit `mode=...`; use the one closed
`config/cortex.json`, not the legacy signed-runtime config format. Mode views
expose only the listed DB roles. Client has no DB roles and exposes only its own
credential reference; status has no secret. No implicit mode, secret, provider or
URL is admitted. Preserve private/no-follow/no-hardlink bounded stable-inode
reads, configured integer request/shutdown bounds, explicit AI off and J08
verify-full. Config has no key material or provider/model selection.

Bob's render consumes those same Core views. Projections are disposable private
single-file copies of the selected purpose references plus root certificate for
DB access; never mount the whole vault. The frozen operator argv and client wire
are published PROPOSALS: Day-1 config source delivers no dispatcher/listener or
projector. In particular, Bob's provisional `CORTEX_CREDENTIAL_FILE` is not a
ratified encrypted-custody or transport API. Cox/Finn/Bob must agree the private
pipe/FD and lifetime before dependent integration. Credentials must never enter
argv, logs or evidence. The wrapper must sanitize inherited Cortex environment,
refuse malformed/duplicate/missing projections and stop before the client on
failure. No legacy discovery, admin scan, URL fallback or second persisted config.

The published J06 proposals are pinned below as PROPOSED. They differ on unlock
FD admission, AAD identity, record layout and lock semantics. Cox must publish a
NEW explicit reconciliation receipt with Finn before dependent source. Neither
proposal is silently accepted here. Stable intent must be encrypted before SQL
WRITES; fresh read-only absence admission is a separately reconciled owner choice.

J06 now requires an explicit encrypted project-vault adapter design FIRST, owned
by Cox/Finn and accepted before build. Their design names the existing crypto
donor or exact dependency, unlock/key source, no-follow layout, crash/replay
state machine, ownership and typed failures. This spec does not choose those
facts. No inferred key, plaintext fallback, extra token store or sole-copy Podman
secret is permitted. Encrypted canonical custody precedes disposable projections.

Finn's published pin contains negative tests and unchanged helper; `seed.py` is
absent. The required sole seed preserves stable intent/IDs/key/secret encrypted
BEFORE SQL, validates actual role/database identities and safe flags/memberships,
uses the existing protected binding/enrollment transaction, then atomically
checkpoints. SQL and filesystem are separate transactions: every crash boundary
must replay the identical credential/IDs or refuse missing custody, never mint a
replacement. Changed intent, same-name/new-OID substitution, unsafe role flags,
concurrent enrollment and serve/installer separation need adverse proof. Initial
identity is human read/write with roles=[]; no request-time or header promotion.
The old 21 migration bytes remain unchanged and migration precedes seed.

The sole helper must refuse missing private projection before ANY HTTP request;
keep typed refusals/nonzero exits, protected credential transmission, one deadline
and explicit identical-key replay after uncertain writes. Existing legacy helper
behavior and negative fixture results do not satisfy these future obligations.
A .005 settings choice cannot change missing/unconfigured into success. No
provider/model/vendor defaults, local AI or provider calls are introduced here.

HALE-A1-NEMO-CI-001 stays OPEN until Mike publishes the existing compat test path
as RUN in his manifest AND the exact composed inventory passes. All new tests
remain in that one path. Inventory is admission, never suite/installed coverage;
hosted results belong only to their exact tested head. Hale closes the finding
in Cox's commissioned new-head round; the author cannot self-close it.

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
  },
  "day2": {
    "published_heads": {
      "cox": "8e1babb2118a7b2ecb11115babb4dd39c90750d6",
      "bob": "887c4d6d3d9e1523d13297b5bab949edb7c48b7c",
      "finn": "bc4514a66164c9a5cbee449d7ff5bd2dc839d74c"
    },
    "source_receipts": {
      "cox_config": {
        "owner": "cox",
        "commit": "8e1babb2118a7b2ecb11115babb4dd39c90750d6",
        "path": "next/src/cortex_core/runtime/bootstrap_config.py",
        "git_blob": "9232c23285f2e27aa3dfd1583583d35623552774",
        "sha256": "23343a2cc50c6a816724fcc027935e56bb2434faa9ef677cc539837a1c93abad"
      },
      "cox_contract": {
        "owner": "cox",
        "commit": "8e1babb2118a7b2ecb11115babb4dd39c90750d6",
        "path": "next/src/cortex_core/runtime/contract.md",
        "git_blob": "d4425bddcfe9026475ca6ca0271d45d534d8ff88",
        "sha256": "248b3485913d4b6c1ad1a5928731e55de36f0c318fa3bb741b6e02afe5948cbe"
      },
      "cox_reader": {
        "owner": "cox",
        "commit": "8e1babb2118a7b2ecb11115babb4dd39c90750d6",
        "path": "next/src/cortex_core/runtime/credentials.py",
        "git_blob": "1b2d751a6cb6c23b84ccf0dec598f9758d7c5322",
        "sha256": "71eb9d2853a1eaf1dea5df7df882da949a4d714272b93b2102fe401cd88ec389"
      },
      "bob_run": {
        "owner": "bob",
        "commit": "887c4d6d3d9e1523d13297b5bab949edb7c48b7c",
        "path": "next/release/ship/run/contract.md",
        "git_blob": "1ab50258e6491db074726de72025cd3344d149c3",
        "sha256": "be6988414f525b0e94c3c794c08efb4aaab398497cbdcba0e1db8f4f0201638c"
      },
      "bob_cli": {
        "owner": "bob",
        "commit": "887c4d6d3d9e1523d13297b5bab949edb7c48b7c",
        "path": "next/release/ship/cli/contract.md",
        "git_blob": "ff14594b7b82d6d5c2c892ed16a0c9d3e5c0cdb3",
        "sha256": "b52342683af3b4304b34d0e52c5d105c3239ea74d81b2d578da56174202d9ed1"
      },
      "finn_negative": {
        "owner": "finn",
        "commit": "bc4514a66164c9a5cbee449d7ff5bd2dc839d74c",
        "path": "next/src/cortex_core/schema/tests/test_seed.py",
        "git_blob": "5c506252374b4f7eafb703de591d24362c6f5e1e",
        "sha256": "c7a1cb02442f0ba9718edbd43395c13198b64186870322ce732f6bef4e44e2ba"
      },
      "finn_helper": {
        "owner": "finn",
        "commit": "bc4514a66164c9a5cbee449d7ff5bd2dc839d74c",
        "path": "packages/cli/_cortex_api.sh",
        "git_blob": "0eb5fc0d6e52d288ec68a39638195e942574b430",
        "sha256": "d78ad0c87ffdf286c69b2485d3224186decc81b37608a4ae92fe40806d84f636"
      }
    },
    "evidence_class": "document assertions against pinned publications; not runtime integration",
    "config": {
      "path": "config/cortex.json",
      "required": [
        "version",
        "installation_id",
        "tenant_id",
        "project_id",
        "project_names",
        "ai_mode",
        "vault_root",
        "bind",
        "publish",
        "client",
        "database",
        "roles",
        "limits"
      ],
      "decoder": "runtime.bootstrap_config.decode_config(raw, mode=mode)",
      "closed_keys": true,
      "extra_parser_or_persisted_client_config": false,
      "mode_purposes": {
        "serve": [
          "api_request",
          "probe"
        ],
        "migrate": [
          "migrator"
        ],
        "seed": [
          "bootstrap",
          "installer_enrollment",
          "api_request"
        ],
        "status": [],
        "client": []
      },
      "client_secret": "client.credential_ref only in client mode",
      "unknown_or_missing_mode": "config_mode",
      "ai_mode": "explicit off; missing=config_shape; other=unshipped_provider_refusal",
      "tls": {
        "mode": "verify-full",
        "root_cert_ref": "database.tls.root_cert_ref",
        "other_mode": "config_transport_unratified"
      },
      "bounds": {
        "config_bytes": 65536,
        "credential_bytes": 4096,
        "request_seconds": [
          1,
          120
        ],
        "shutdown_seconds": [
          1,
          120
        ]
      }
    },
    "projections": {
      "reference_pattern": "secrets/cortex/[a-zA-Z0-9_-]{1,96}",
      "reader": [
        "owned",
        "private",
        "regular",
        "no_symlink_any_component",
        "no_hardlinks",
        "bounded",
        "stable_inode"
      ],
      "purposes_source": "config.mode_purposes",
      "add_root_cert_for_database": true,
      "whole_vault_mount": false,
      "authoritative_secret_copy": "encrypted project vault"
    },
    "operator": {
      "state": "published proposal; dispatcher not delivered by foundation",
      "argv": [
        "/opt/cortex/python/bin/python3",
        "-I",
        "-B",
        "-m",
        "cortex_core.cli",
        "<serve|migrate|seed|status>",
        "--config",
        "/run/cortex/config/cortex.json"
      ],
      "bootstrap_order": [
        "core-init",
        "temporary-db",
        "roles-pre",
        "migrate",
        "roles-post",
        "seed",
        "roles-close",
        "stop-temporary-db",
        "serve"
      ],
      "retries": 0
    },
    "client_join": {
      "state": "OPEN: Cox/Finn/Bob reconciliation before integration",
      "bob_proposed_keys": [
        "CORTEX_API_URL",
        "CORTEX_PROJECT",
        "CORTEX_CREDENTIAL_FILE"
      ],
      "core_command": "cortex config client --config FILE",
      "secret_transport": "protected pipe/FD; no secret argv/log/temp evidence",
      "installed_credit": 0,
      "infer_credential_file_acceptance": false
    },
    "seed": {
      "entry": "seed_runtime_bindings(config, operation_key, deadline)",
      "published_implementation": "ABSENT at Finn Day-1 pin",
      "owner": "finn",
      "first_identity": {
        "kind": "human",
        "grant": "read/write",
        "roles": []
      },
      "request_time_enrollment": false,
      "migration_prefix": "unchanged 21; migrate before seed",
      "sequence": [
        "persist stable IDs/operation key/secret encrypted before SQL",
        "validate actual login and database OIDs/owner/flags/memberships",
        "bind and enroll through existing protected SQL",
        "atomically checkpoint after commit"
      ],
      "lost_ack": "reuse identical intent/secret/IDs; no replacement credential",
      "missing_custody": "custody_unavailable; no replacement credential",
      "required_cases": [
        "exact_retry",
        "changed_intent",
        "every_crash_boundary",
        "same_name_new_oid",
        "unsafe_role_or_membership",
        "concurrent_seed",
        "serve_separation"
      ]
    },
    "custody": {
      "gate": "Cox/Finn explicit encrypted-adapter design accepted before build",
      "required_design": [
        "crypto_donor_or_dependency",
        "unlock_key_source",
        "nofollow_layout",
        "crash_replay_state_machine",
        "ownership",
        "typed_failures"
      ],
      "inferred_defaults": [],
      "plaintext_fallback": false,
      "second_store": false,
      "design_state": "PROPOSED; accepted reconciliation receipt required",
      "reconciliation_owner": "cox with finn",
      "open_reconciliation": [
        "unlock_protocol",
        "AAD_identity",
        "record_reference",
        "lock_semantics",
        "absence_observation",
        "adapter_and_client_api"
      ],
      "pre_sql_scope": "before mutating SQL; fresh read-only admission needs owner ruling",
      "drafts_authorize_build": false,
      "design_receipts": {
        "J06-custody-adapter.md": {
          "path": "/Volumes/WD-B-4TB/DevVault/helix/review/cortex/design/J06-custody-adapter.md",
          "sha256": "196c954b776ed4c032d762df5691f0f93bcbdba9bce78486ca4ea431081f21c6",
          "status": "PROPOSED"
        },
        "J06-seed-replay.md": {
          "path": "/Volumes/WD-B-4TB/DevVault/helix/review/cortex/design/J06-seed-replay.md",
          "sha256": "3ce2cdf46638e4bfdd95292e8e2fe48698bd0afe82eea9a8b1b1e8d05ae15dfe",
          "status": "PROPOSED"
        }
      }
    },
    "transport": {
      "owner_path": "packages/cli/_cortex_api.sh",
      "missing_projection_http_attempts": 0,
      "discovery_admin_v1_fallback": false,
      "mutation_retry": "explicit identical replay with same key only",
      "refusal": "preserve typed packet; nonzero exit; no fabricated success",
      "seed_or_helper_fixture_as_installed": false
    },
    "provider_settings": "deferred .005; missing/unconfigured remains typed refusal",
    "ci": {
      "finding": "HALE-A1-NEMO-CI-001",
      "owner": "mike",
      "manifest": ".github/next-offline-suites.json",
      "required_run": "next/src/cortex_core/compat/tests/test_contract.py",
      "closure": "published RUN addition AND exact composed inventory pass; Hale adjudicates",
      "inventory_is_suite_or_installed_pass": false,
      "hosted_acceptance_inherited": false
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
