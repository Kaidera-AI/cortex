"""Explicit forward-only migration port; never invoked by API startup."""
import hashlib
import json
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[2] / "schema"


class MigrationError(ValueError):
    """Untrusted migration input or an incompatible applied ledger."""


def apply_migrations(connection, directory=SCHEMA, *, through=None):
    if not connection.autocommit:
        raise MigrationError("Migration connection must use autocommit")
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    if type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise MigrationError("Unsupported migration manifest")
    prepared = []
    seen = set()
    for entry in manifest["migrations"]:
        file = (directory / entry["file"]).resolve()
        if not file.is_relative_to(directory) or entry["id"] in seen:
            raise MigrationError("Invalid migration path or duplicate ID")
        data = file.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise MigrationError("Migration checksum differs")
        prepared.append((entry["id"], entry["sha256"], data.decode("utf-8")))
        seen.add(entry["id"])
    if through is not None:
        if through not in seen:
            raise MigrationError("Unknown migration target")
        prepared_target = next(i for i, item in enumerate(prepared) if item[0] == through)
    else:
        prepared_target = len(prepared) - 1
    applied = []
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(6203001)")
        connection.execute("CREATE SCHEMA IF NOT EXISTS core")
        connection.execute("""CREATE TABLE IF NOT EXISTS core.schema_migrations (
            migration_id text PRIMARY KEY, sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            applied_at timestamptz NOT NULL DEFAULT now())""")
        ledger = dict(connection.execute("SELECT migration_id,sha256 FROM core.schema_migrations").fetchall())
        if set(ledger) - seen:
            raise MigrationError("Applied migration is absent; downgrade refused")
        for identity, digest, _ in prepared:
            if identity in ledger and ledger[identity] != digest:
                raise MigrationError("Applied migration checksum differs")
        ledger_ids = set(ledger)
        if ledger_ids != {item[0] for item in prepared[:len(ledger_ids)]}:
            raise MigrationError("Applied migration ledger is not a prefix")
        for identity, digest, sql in prepared[:prepared_target + 1]:
            if identity in ledger:
                continue
            connection.execute(sql, prepare=False)
            connection.execute("INSERT INTO core.schema_migrations (migration_id,sha256) VALUES (%s,%s)", (identity, digest))
            applied.append(identity)
    return applied
