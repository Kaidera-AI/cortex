"""C03 semantic mutants; SQL checksums refreshed so checks reach PostgreSQL."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import psycopg

NEXT = Path(__file__).resolve().parents[1]
MANIFEST = NEXT / "schema/manifest.json"
sys.path.insert(0, str(NEXT / "scripts"))
from test_receipts import classify, report, suite as receipt_suite


def suite():
    return receipt_suite(NEXT / "tests/schema")


def checkpoint():
    # Bound WAL in the explicitly disposable stack after repeated full DDL suites.
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as connection:
        connection.execute("CHECKPOINT")


def mutation_killed(result, expected=()):
    """Strict boolean adapter: an inconclusive result invalidates proof."""
    status = mutation_status(result, expected)
    if status == "inconclusive":
        raise SystemExit("Inconclusive test receipt invalidates mutation proof")
    return status == "killed"


def mutation_status(result, expected):
    return classify(result, expected)


def run():
    baseline = suite()
    if mutation_status(baseline, set()) != "survived":
        print(baseline.stdout + baseline.stderr)
        raise SystemExit("Mutation baseline is RED")
    checkpoint()
    mutations = [
        ("src/cortex_core/migrations.py", "checksum validation removed", 'if hashlib.sha256(data).hexdigest() != entry["sha256"]:', "if False:"),
        ("src/cortex_core/migrations.py", "applied drift accepted", 'if identity in ledger and ledger[identity] != digest:', "if False:"),
        ("src/cortex_core/migrations.py", "noninteger manifest accepted", 'type(manifest.get("version")) is not int or ', ""),
        ("src/cortex_core/migrations.py", "ledger preflight delayed", '        for identity, digest, _ in prepared:', ''),
        ("src/cortex_core/migrations.py", "transaction removed", 'with connection.transaction():', 'with __import__("contextlib").nullcontext():'),
        ("schema/core/001-core.sql", "payload hash unbound", "CHECK (sha256 = encode(sha256(body), 'hex'))", "CHECK (true)"),
        ("schema/core/001-core.sql", "head history unchecked", "ADD CONSTRAINT record_head_has_history", "ADD CONSTRAINT record_head_has_history"),
        ("schema/core/001-core.sql", "history updates accepted", "RAISE EXCEPTION 'canonical history is immutable' USING ERRCODE = '55000';", "RETURN NEW;"),
        ("schema/auth/001-auth.sql", "untyped grants admitted", "permissions <@ ARRAY['read','write','control','owner','admin']", "true"),
        ("schema/coordination/001-coordination.sql", "fence regression accepted", "IF NEW.fence < OLD.fence THEN", "IF false THEN"),
        ("schema/coordination/001-coordination.sql", "tombstone mismatch admitted", "CHECK ((operation = 'delete') = tombstone)", "CHECK (true)"),
        ("schema/coordination/001-coordination.sql", "retention default changed", "DEFAULT 604800", "DEFAULT 3600"),
        ("schema/coordination/001-coordination.sql", "consumer expiry shortened", "interval '7 days'", "interval '1 day'"),
        ("schema/coordination/001-coordination.sql", "revision payload binding removed", "FOREIGN KEY (tenant_id, project_id, aggregate_id, aggregate_revision, tombstone, payload_ref) REFERENCES core.record_revisions (tenant_id, project_id, record_id, revision, tombstone, payload_ref)", "FOREIGN KEY (tenant_id, project_id, aggregate_id, aggregate_revision, tombstone) REFERENCES core.record_revisions (tenant_id, project_id, record_id, revision, tombstone)"),
        ("schema/coordination/001-coordination.sql", "publication installation binding removed", "FOREIGN KEY (installation_id, tenant_id, project_id, event_id) REFERENCES coordination.outbox (installation_id, tenant_id, project_id, event_id)", "FOREIGN KEY (tenant_id, project_id, event_id) REFERENCES coordination.outbox (tenant_id, project_id, event_id)"),
        ("schema/retrieval/000-canonical.sql", "nonfinite embeddings admitted", "retrieval.finite_vector(embedding)", "true"),
        ("schema/retrieval/000-canonical.sql", "embedding dimensions unbound", "cardinality(embedding) = dimensions", "true"),
        ("schema/manifest.json", "unsupported manifest version", '"version": 1', '"version": 2'),
        ("scripts/mutate_schema.py", "runner failure counted as kill", "def mutation_status(result, expected):\n", "def mutation_status(result, expected):\n    return 'killed' if result.returncode else 'survived'\n"),
    ]
    expected = {
        "checksum validation removed": "test_schema.SchemaTests.test_manifest_tamper_refused_before_ddl",
        "applied drift accepted": "test_schema.SchemaTests.test_ledger_idempotence_and_applied_drift_refused",
        "noninteger manifest accepted": "test_integrity.IntegrityTests.test_noninteger_manifest_version_refused",
        "ledger preflight delayed": "test_integrity.IntegrityTests.test_all_applied_hashes_checked_before_any_pending_sql",
        "transaction removed": "test_schema.SchemaTests.test_migration_failure_rolls_back_whole_install",
        "payload hash unbound": "test_schema.SchemaTests.test_payload_digest_and_immutability",
        "head history unchecked": "test_schema.SchemaTests.test_head_without_history_fails_at_commit",
        "history updates accepted": "test_schema.SchemaTests.test_revision_tombstone_and_history_immutable",
        "untyped grants admitted": "test_schema.SchemaTests.test_principal_credential_and_project_grant_scope",
        "fence regression accepted": "test_schema.SchemaTests.test_fence_never_regresses",
        "tombstone mismatch admitted": "test_schema.SchemaTests.test_outbox_shape_tombstone_digest_and_immutability",
        "retention default changed": "test_schema.SchemaTests.test_feed_retention_floor_and_publication_identity",
        "consumer expiry shortened": "test_schema.SchemaTests.test_feed_retention_floor_and_publication_identity",
        "revision payload binding removed": "test_integrity.IntegrityTests.test_outbox_payload_matches_that_exact_revision",
        "publication installation binding removed": "test_integrity.IntegrityTests.test_publication_installation_matches_event_installation",
        "nonfinite embeddings admitted": "test_schema.SchemaTests.test_embedding_model_dimensions_and_source_revision",
        "embedding dimensions unbound": "test_schema.SchemaTests.test_embedding_model_dimensions_and_source_revision",
        "unsupported manifest version": "test_schema.SchemaTests.test_schemas_and_future_fact_slots_exist",
        "runner failure counted as kill": "test_causal_mutations.TeamRule.test_runner_errors_and_missing_receipts_inconclusive",
    }
    survivors, inconclusive = [], []
    for relative, label, before, after in mutations:
        path = NEXT / relative
        original = path.read_bytes()
        original_manifest = MANIFEST.read_bytes()
        source = original.decode()
        assert before in source, label
        if label == "head history unchecked":
            start = source.index("ALTER TABLE core.records ADD CONSTRAINT")
            end = source.index("CREATE TRIGGER revision_immutable", start)
            changed = source[:start] + source[end:]
        elif label == "ledger preflight delayed":
            preflight = '        for identity, digest, _ in prepared:\n            if identity in ledger and ledger[identity] != digest:\n                raise MigrationError("Applied migration checksum differs")\n'
            changed = source.replace(preflight, '', 1).replace('            if identity in ledger:\n                continue', '            if identity in ledger:\n                if ledger[identity] != digest:\n                    raise MigrationError("Applied migration checksum differs")\n                continue', 1)
        else:
            changed = source.replace(before, after, 1)
        try:
            path.write_text(changed)
            if path.suffix == ".sql":
                manifest = json.loads(original_manifest)
                for entry in manifest["migrations"]:
                    if NEXT / "schema" / entry["file"] == path:
                        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                MANIFEST.write_text(json.dumps(manifest))
            result = suite()
            status = mutation_status(result, {expected[label]})
            print(json.dumps({"source": relative, "mutation": label, "expected_test": expected[label], "status": status, "exit_code": result.returncode, "receipt": report(result), "stdout": result.stdout, "stderr": result.stderr}), flush=True)
            if status == "survived":
                survivors.append(label)
            elif status == "inconclusive":
                inconclusive.append(label)
        finally:
            path.write_bytes(original)
            MANIFEST.write_bytes(original_manifest)
        checkpoint()
    print(json.dumps({"mutants": len(mutations), "killed": len(mutations) - len(survivors) - len(inconclusive), "survivors": survivors, "inconclusive": inconclusive}), flush=True)
    final = suite()
    print(json.dumps({"restored_baseline_exit": final.returncode, "restored_baseline_output": final.stdout + final.stderr}), flush=True)
    if survivors or inconclusive or mutation_status(final, set()) != "survived":
        raise SystemExit(1)


if __name__ == "__main__":
    run()
