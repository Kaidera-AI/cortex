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


def suite():
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(NEXT / "tests/schema")], capture_output=True, text=True)


def checkpoint():
    # Bound WAL in the explicitly disposable stack after repeated full DDL suites.
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as connection:
        connection.execute("CHECKPOINT")


def mutation_killed(result):
    if result.returncode < 0:
        raise SystemExit("Signal termination invalidates mutation proof")
    output = result.stdout + result.stderr
    if any(marker in output for marker in ("psycopg.OperationalError", "psycopg.errors.DiskFull", "psycopg.errors.OutOfMemory")):
        raise SystemExit("Operational failure invalidates mutation proof")
    return result.returncode != 0


def mutation_status(result, expected):
    # RED-first extraction of the previous predicate before replacing it.
    try:
        return "killed" if mutation_killed(result) else "survived"
    except SystemExit:
        return "inconclusive"


def run():
    baseline = suite()
    if baseline.returncode:
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
        ("scripts/mutate_schema.py", "signal termination counted as kill", "if result.returncode < 0:", "if False:"),
    ]
    survivors = []
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
            try:
                killed = mutation_killed(result)
            except SystemExit:
                print(result.stdout + result.stderr, flush=True)
                raise
            print(json.dumps({"source": relative, "mutation": label, "killed": killed, "exit_code": result.returncode, "output_tail": (result.stdout + result.stderr)[-1800:]}), flush=True)
            if not killed:
                survivors.append(label)
        finally:
            path.write_bytes(original)
            MANIFEST.write_bytes(original_manifest)
        checkpoint()
    print(json.dumps({"mutants": len(mutations), "killed": len(mutations) - len(survivors), "survivors": survivors}), flush=True)
    final = suite()
    print(json.dumps({"restored_baseline_exit": final.returncode, "restored_baseline_output": final.stdout + final.stderr}), flush=True)
    if survivors or final.returncode:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
