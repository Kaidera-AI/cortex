"""Explicit forward-only migration port; never invoked by API startup."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[2] / "schema"


class MigrationError(ValueError):
    """Untrusted migration input or an incompatible applied ledger."""


def apply_migrations(connection, directory=SCHEMA, *, through=None, _boundary=None, _transaction_scope=None, _execute=None):
    from psycopg.pq import TransactionStatus

    def require_state(expected):
        try:
            autocommit = connection.autocommit
            status = connection.info.transaction_status
        except Exception:
            raise MigrationError("Migration connection state unavailable") from None
        if autocommit is not True or type(status) is not TransactionStatus or status is not expected:
            raise MigrationError(f"Migration connection must use autocommit and driver {expected.name}")

    def boundary(stage):
        if _boundary is not None:
            _boundary(stage, connection)

    def execute(*args, **kwargs):
        boundary('before_sql')
        result = (_execute if _execute is not None else connection.execute)(*args, **kwargs)
        boundary('after_sql')
        return result

    boundary('before_prepare')
    require_state(TransactionStatus.IDLE)
    directory = Path(directory).resolve()
    boundary('before_read')
    manifest = json.loads((directory / "manifest.json").read_text())
    boundary('after_read')
    if type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise MigrationError("Unsupported migration manifest")
    prepared = []
    seen = set()
    for entry in manifest["migrations"]:
        file = (directory / entry["file"]).resolve()
        if not file.is_relative_to(directory) or entry["id"] in seen:
            raise MigrationError("Invalid migration path or duplicate ID")
        boundary('before_read')
        data = file.read_bytes()
        boundary('after_read')
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise MigrationError("Migration checksum differs")
        prepared.append((entry["id"], entry["sha256"], data.decode("utf-8")))
        seen.add(entry["id"])
    if through is not None:
        if type(through) is not str or through not in seen:
            raise MigrationError("Unknown migration target")
        prepared_target = next(i for i, item in enumerate(prepared) if item[0] == through)
    else:
        prepared_target = len(prepared) - 1
    boundary('after_prepare')
    applied = []
    # Preparation must not borrow a transaction opened by the caller meanwhile.
    require_state(TransactionStatus.IDLE)
    with connection.transaction() as transaction:
        try:
            savepoint_name = transaction.savepoint_name
        except Exception:
            raise MigrationError("Migration transaction ownership unavailable") from None
        # psycopg uses an empty string for an unnamed top-level transaction.
        if type(savepoint_name) is not str or savepoint_name != "":
            raise MigrationError("Migration transaction must own the top-level commit")
        require_state(TransactionStatus.INTRANS)
        with (_transaction_scope(connection, transaction) if _transaction_scope is not None else nullcontext()):
            execute("SELECT pg_advisory_xact_lock(6203001)")
            execute("CREATE SCHEMA IF NOT EXISTS core")
            execute("""CREATE TABLE IF NOT EXISTS core.schema_migrations (
                migration_id text PRIMARY KEY, sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
                applied_at timestamptz NOT NULL DEFAULT now())""")
            ledger = dict(execute("SELECT migration_id,sha256 FROM core.schema_migrations").fetchall())
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
                execute(sql, prepare=False)
                execute("INSERT INTO core.schema_migrations (migration_id,sha256) VALUES (%s,%s)", (identity, digest))
                applied.append(identity)
        boundary('before_commit')
    boundary('after_commit')
    require_state(TransactionStatus.IDLE)
    return applied
