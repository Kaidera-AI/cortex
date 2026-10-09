"""C03 semantic mutants; SQL checksums refreshed so checks reach PostgreSQL."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

NEXT = Path(__file__).resolve().parents[1]
MANIFEST = NEXT / "schema/manifest.json"


def suite():
    return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(NEXT / "tests/schema")], capture_output=True, text=True)


def run():
    baseline = suite()
    if baseline.returncode:
        print(baseline.stdout + baseline.stderr)
        raise SystemExit("Mutation baseline is RED")
    mutations = [
        ("src/cortex_core/migrations.py", "checksum validation removed", 'if hashlib.sha256(data).hexdigest() != entry["sha256"]:', "if False:"),
        ("src/cortex_core/migrations.py", "applied drift accepted", 'if ledger[identity] != digest:', "if False:"),
        ("src/cortex_core/migrations.py", "transaction removed", 'with connection.transaction():', 'with __import__("contextlib").nullcontext():'),
        ("schema/core/001-core.sql", "payload hash unbound", "CHECK (sha256 = encode(sha256(body), 'hex'))", "CHECK (true)"),
        ("schema/core/001-core.sql", "head history unchecked", "ADD CONSTRAINT record_head_has_history", "ADD CONSTRAINT record_head_has_history"),
        ("schema/core/001-core.sql", "history updates accepted", "RAISE EXCEPTION 'canonical history is immutable' USING ERRCODE = '55000';", "RETURN NEW;"),
        ("schema/auth/001-auth.sql", "untyped grants admitted", "permissions <@ ARRAY['read','write','control','owner','admin']", "true"),
        ("schema/coordination/001-coordination.sql", "fence regression accepted", "IF NEW.fence < OLD.fence THEN", "IF false THEN"),
        ("schema/coordination/001-coordination.sql", "tombstone mismatch admitted", "CHECK ((operation = 'delete') = tombstone)", "CHECK (true)"),
        ("schema/coordination/001-coordination.sql", "retention default changed", "DEFAULT 604800", "DEFAULT 3600"),
        ("schema/coordination/001-coordination.sql", "consumer expiry shortened", "interval '7 days'", "interval '1 day'"),
        ("schema/retrieval/000-canonical.sql", "nonfinite embeddings admitted", "retrieval.finite_vector(embedding)", "true"),
        ("schema/retrieval/000-canonical.sql", "embedding dimensions unbound", "cardinality(embedding) = dimensions", "true"),
        ("schema/manifest.json", "unsupported manifest version", '"version": 1', '"version": 2'),
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
            killed = result.returncode != 0
            print(json.dumps({"source": relative, "mutation": label, "killed": killed, "exit_code": result.returncode, "output_tail": (result.stdout + result.stderr)[-1800:]}), flush=True)
            if not killed:
                survivors.append(label)
        finally:
            path.write_bytes(original)
            MANIFEST.write_bytes(original_manifest)
    print(json.dumps({"mutants": len(mutations), "killed": len(mutations) - len(survivors), "survivors": survivors}), flush=True)
    if survivors or suite().returncode:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
