import importlib.util
import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
REPO_ROOT = Path(__file__).resolve().parents[3]


def load_api_module():
    spec = importlib.util.spec_from_file_location(
        "cortex_api_main_schema_migrations_test",
        API_MAIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MigrationTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.applied = dict(self.conn.applied)
        self.executed = list(self.conn.executed_migration_sql)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.applied = self.applied
            self.conn.executed_migration_sql = self.executed
        return False


class MigrationConn:
    def __init__(self, applied=None, *, hnsw_replacements=(), fail_ledger=False):
        self.applied = dict(applied or {})
        self.hnsw_replacements = set(hnsw_replacements)
        self.executed_migration_sql = []
        self.fail_ledger = fail_ledger

    def transaction(self):
        return MigrationTransaction(self)

    async def fetch(self, sql, *args):
        if "FROM cortex_schema_migrations" in sql:
            return list(self.applied.values())
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql, *args):
        if "FROM pg_catalog.pg_class AS idx" in sql:
            return {"ready": True} if tuple(args) in self.hnsw_replacements else None
        if "FROM cortex_schema_migrations" in sql and "WHERE migration_id" in sql:
            return self.applied.get(args[0])
        raise AssertionError(f"Unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            return None
        raise AssertionError(f"Unexpected fetchval SQL: {sql}")

    async def execute(self, sql, *args):
        if "CREATE TABLE IF NOT EXISTS cortex_schema_migrations" in sql:
            return "CREATE TABLE"
        if "ALTER TABLE cortex_schema_migrations OWNER TO postgres" in sql:
            return "ALTER TABLE"
        if "GRANT SELECT ON TABLE cortex_schema_migrations" in sql:
            return "DO"
        if "INSERT INTO cortex_schema_migrations" in sql:
            if self.fail_ledger:
                raise RuntimeError("injected ledger write failure")
            migration_id, checksum, source_path, applied_by, statement_status, surface_version = args
            self.applied[migration_id] = {
                "migration_id": migration_id,
                "checksum_sha256": checksum,
                "source_path": source_path,
                "applied_by": applied_by,
                "applied_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "statement_status": statement_status,
                "surface_version": surface_version,
            }
            return "INSERT 0 1"
        if "SELECT 1 AS migration_test" in sql:
            self.executed_migration_sql.append(sql)
            return "SELECT 1"
        raise AssertionError(f"Unexpected execute SQL: {sql}")


class OnlineIndexConn(MigrationConn):
    def __init__(self, *args, indexes=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.indexes = dict(indexes or {})
        self.session_lock_calls = []

    async def fetchrow(self, sql, *args):
        if "pg_get_indexdef" in sql:
            return self.indexes.get((args[0], args[1]))
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        if "pg_advisory_lock(" in sql:
            self.session_lock_calls.append(("lock", args[0]))
            return None
        if "pg_advisory_unlock(" in sql:
            self.session_lock_calls.append(("unlock", args[0]))
            return True
        return await super().fetchval(sql, *args)

    async def execute(self, sql, *args):
        if "CREATE INDEX CONCURRENTLY" in sql:
            match = re.search(r"CREATE INDEX CONCURRENTLY IF NOT EXISTS\s+(\w+)", sql)
            assert match is not None
            index_name = match.group(1)
            table = re.search(r"\bON\s+public\.(\w+)", sql)
            assert table is not None
            definition = sql[sql.index("CREATE INDEX CONCURRENTLY") :]
            self.indexes[("public", index_name)] = {
                "table_schema": "public",
                "table_name": table.group(1),
                "valid": True,
                "ready": True,
                "definition": definition,
            }
            self.executed_migration_sql.append(sql)
            return "CREATE INDEX"
        if "DROP INDEX CONCURRENTLY" in sql:
            match = re.search(
                r"DROP INDEX CONCURRENTLY IF EXISTS\s+(?:public\.|\"public\"\.)?\"?(\w+)\"?",
                sql,
            )
            assert match is not None
            self.indexes.pop(("public", match.group(1)), None)
            self.executed_migration_sql.append(sql)
            return "DROP INDEX"
        return await super().execute(sql, *args)


def write_migration(root: Path, name: str, sql: str = "SELECT 1 AS migration_test;\n") -> Path:
    path = root / name
    path.write_text(sql, encoding="utf-8")
    return path


def applied_row(migration_id: str, path: Path, checksum: str) -> dict:
    return {
        "migration_id": migration_id,
        "checksum_sha256": checksum,
        "source_path": str(path),
        "applied_by": "old-runner",
        "applied_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "statement_status": "SELECT 1",
        "surface_version": "old",
    }


def test_applied_roster_migration_remains_byte_immutable():
    migration = (
        REPO_ROOT
        / ".agents/data/migrations/2026-06-01-roster-as-data.sql"
    ).read_bytes()

    assert hashlib.sha256(migration).hexdigest() == (
        "5d29fc6b69840b7139674aeabd96495034a7e04ddabf35de474c7778e99233e8"
    )


@pytest.mark.asyncio
async def test_unfiltered_plan_accepts_recorded_roster_migration_checksum():
    api = load_api_module()
    migration = (
        REPO_ROOT
        / ".agents/data/migrations/2026-06-01-roster-as-data.sql"
    )
    checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
    conn = MigrationConn(
        {
            migration.name: applied_row(
                migration.name,
                migration,
                checksum,
            )
        }
    )

    plan = await api.schema_migration_plan(conn, migration_dir=migration.parent)
    roster = next(row for row in plan["migrations"] if row["id"] == migration.name)

    assert roster["status"] == "applied"
    assert roster["applied_migration_id"] == migration.name


@pytest.mark.asyncio
async def test_v02001_comment_only_roster_checksum_is_an_exact_compatibility_alias():
    api = load_api_module()
    migration = (
        REPO_ROOT
        / ".agents/data/migrations/2026-06-01-roster-as-data.sql"
    )
    old_release_checksum = (
        "26f8e808b9f4b38542ca6a9e79ddad153f78db8855ed2ea3d8ea789e5f0c2da5"
    )
    conn = MigrationConn(
        {
            migration.name: applied_row(
                migration.name,
                migration,
                old_release_checksum,
            )
        }
    )

    plan = await api.schema_migration_plan(conn, migration_dir=migration.parent)
    roster = next(row for row in plan["migrations"] if row["id"] == migration.name)

    assert roster["status"] == "applied"


def test_applied_blocking_ivfflat_migration_remains_byte_immutable():
    migration = (
        REPO_ROOT
        / ".agents/data/migrations/2026-08-16-03-drop-degenerate-ivfflat-indexes.sql"
    ).read_bytes()

    assert hashlib.sha256(migration).hexdigest() == (
        "5d0d3359ceea281cc73438ff9814a2f5a45ce3f01fda6bde7b96956b6c902f18"
    )


@pytest.mark.asyncio
async def test_apply_schema_migrations_dry_run_lists_pending_without_execution(tmp_path):
    api = load_api_module()
    write_migration(tmp_path, "2026-06-01-alpha.sql")
    write_migration(tmp_path, "2026-06-02-beta.sql")
    conn = MigrationConn()

    result = await api.apply_schema_migrations(conn, dry_run=True, migration_dir=tmp_path)

    assert result["dry_run"] is True
    assert result["applied_count"] == 0
    assert [row["id"] for row in result["results"]] == [
        "2026-06-01-alpha.sql",
        "2026-06-02-beta.sql",
    ]
    assert {row["action"] for row in result["results"]} == {"would_apply"}
    assert conn.executed_migration_sql == []


@pytest.mark.asyncio
async def test_apply_schema_migrations_executes_and_records_ledger(tmp_path):
    api = load_api_module()
    write_migration(tmp_path, "2026-06-01-alpha.sql")
    conn = MigrationConn()

    applied = await api.apply_schema_migrations(
        conn,
        dry_run=False,
        migration_dir=tmp_path,
        applied_by="test-runner",
    )
    rerun = await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert applied["applied_count"] == 1
    assert applied["results"][0]["action"] == "applied"
    assert len(conn.executed_migration_sql) == 1
    assert conn.applied["2026-06-01-alpha.sql"]["applied_by"] == "test-runner"
    assert conn.applied["2026-06-01-alpha.sql"]["surface_version"] == api.CORTEX_SURFACE_VERSION
    assert rerun["applied_count"] == 0
    assert rerun["results"][0]["action"] == "skip_applied"


@pytest.mark.asyncio
async def test_migration_sql_and_ledger_commit_or_roll_back_as_one_transaction(tmp_path):
    api = load_api_module()
    migration = write_migration(tmp_path, "2026-06-01-atomic.sql")
    conn = MigrationConn(fail_ledger=True)

    with pytest.raises(RuntimeError, match="ledger write failure"):
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert conn.executed_migration_sql == []
    assert migration.name not in conn.applied

    conn.fail_ledger = False
    result = await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)
    assert result["applied_count"] == 1
    assert conn.executed_migration_sql == [migration.read_text(encoding="utf-8")]
    assert migration.name in conn.applied


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX CONCURRENTLY idx_x ON example (id);\n",
        "BEGIN;\nSELECT 1;\nCOMMIT;\n",
    ],
)
async def test_nontransactional_forward_migration_is_held_before_any_sql(tmp_path, sql):
    api = load_api_module()
    migration = write_migration(tmp_path, "2026-06-01-needs-protocol.sql", sql)
    conn = MigrationConn()

    with pytest.raises(HTTPException, match="crash-safe nontransactional") as exc:
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert exc.value.status_code == 409
    assert conn.executed_migration_sql == []
    assert migration.name not in conn.applied


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "migration_name",
    [
        "2026-08-17-02-archive-messages-raw-session-index.sql",
        "2026-08-18-03-messages-project-ts-hot-path-index.sql",
    ],
)
async def test_reviewed_online_create_reconciles_sql_success_ledger_failure(
    tmp_path, migration_name
):
    api = load_api_module()
    source = REPO_ROOT / ".agents/data/migrations" / migration_name
    migration = tmp_path / migration_name
    shutil.copyfile(source, migration)
    protocol = api.REVIEWED_ONLINE_INDEX_MIGRATIONS[migration_name]
    conn = OnlineIndexConn(fail_ledger=True)

    with pytest.raises(RuntimeError, match="ledger write failure"):
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    state = conn.indexes[(protocol["schema"], protocol["index"])]
    assert state["valid"] is True and state["ready"] is True
    assert migration_name not in conn.applied
    assert len(conn.executed_migration_sql) == 1

    conn.fail_ledger = False
    result = await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)
    assert result["applied_count"] == 1
    assert result["results"][0]["action"] == "reconciled"
    assert migration_name in conn.applied
    assert len(conn.executed_migration_sql) == 1, "retry must not replay completed DDL"
    assert [kind for kind, _key in conn.session_lock_calls] == [
        "lock", "unlock", "lock", "unlock"
    ]


@pytest.mark.asyncio
async def test_reviewed_online_drop_reconciles_absence_after_ledger_failure(tmp_path):
    api = load_api_module()
    migration_name = "2026-08-17-04-drop-work-products-embedding-ivfflat.sql"
    source = REPO_ROOT / ".agents/data/migrations" / migration_name
    migration = tmp_path / migration_name
    shutil.copyfile(source, migration)
    protocol = api.REVIEWED_ONLINE_INDEX_MIGRATIONS[migration_name]
    conn = OnlineIndexConn(
        fail_ledger=True,
        hnsw_replacements={("idx_work_products_embedding_hnsw", "work_products")},
        indexes={
            (protocol["schema"], protocol["index"]): {
                "table_schema": protocol["schema"],
                "table_name": protocol["table"],
                "valid": True,
                "ready": True,
                "definition": protocol["definition"],
            }
        },
    )

    with pytest.raises(RuntimeError, match="ledger write failure"):
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)
    assert (protocol["schema"], protocol["index"]) not in conn.indexes
    assert migration_name not in conn.applied

    conn.fail_ledger = False
    result = await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)
    assert result["results"][0]["action"] == "reconciled"
    assert migration_name in conn.applied
    assert len(conn.executed_migration_sql) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_definition",
    [
        "CREATE INDEX idx_work_products_embedding ON public.work_products USING btree (project)",
        "CREATE UNIQUE INDEX idx_work_products_embedding ON public.work_products USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1)",
        "CREATE INDEX idx_work_products_embedding ON public.work_products USING ivfflat (project vector_cosine_ops) WITH (lists = 1)",
        "CREATE INDEX idx_work_products_embedding ON public.work_products USING ivfflat (embedding vector_l2_ops) WITH (lists = 1)",
        "CREATE INDEX idx_work_products_embedding ON public.work_products USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1) WHERE project = 'dev-os'",
        "CREATE INDEX idx_work_products_embedding ON public.work_products USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10)",
    ],
)
async def test_reviewed_online_drop_refuses_same_name_wrong_definition_without_mutation(
    tmp_path, wrong_definition
):
    api = load_api_module()
    migration_name = "2026-08-17-04-drop-work-products-embedding-ivfflat.sql"
    shutil.copyfile(
        REPO_ROOT / ".agents/data/migrations" / migration_name,
        tmp_path / migration_name,
    )
    protocol = api.REVIEWED_ONLINE_INDEX_MIGRATIONS[migration_name]
    conn = OnlineIndexConn(
        hnsw_replacements={("idx_work_products_embedding_hnsw", "work_products")},
        indexes={
            (protocol["schema"], protocol["index"]): {
                "table_schema": protocol["schema"],
                "table_name": protocol["table"],
                "valid": True,
                "ready": True,
                "definition": wrong_definition,
            }
        },
    )

    with pytest.raises(HTTPException, match="mismatched catalog identity"):
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)
    assert conn.executed_migration_sql == []
    assert migration_name not in conn.applied
    assert (protocol["schema"], protocol["index"]) in conn.indexes


@pytest.mark.asyncio
@pytest.mark.parametrize(("valid", "ready"), [(False, True), (True, False)])
async def test_reviewed_online_drop_requires_exact_valid_ready_obsolete_index(
    tmp_path, valid, ready
):
    api = load_api_module()
    migration_name = "2026-08-17-04-drop-work-products-embedding-ivfflat.sql"
    shutil.copyfile(
        REPO_ROOT / ".agents/data/migrations" / migration_name,
        tmp_path / migration_name,
    )
    protocol = api.REVIEWED_ONLINE_INDEX_MIGRATIONS[migration_name]
    conn = OnlineIndexConn(
        hnsw_replacements={("idx_work_products_embedding_hnsw", "work_products")},
        indexes={
            (protocol["schema"], protocol["index"]): {
                "table_schema": protocol["schema"],
                "table_name": protocol["table"],
                "valid": valid,
                "ready": ready,
                "definition": protocol["definition"],
            }
        },
    )
    with pytest.raises(HTTPException, match="mismatched catalog identity"):
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)
    assert conn.executed_migration_sql == []
    assert migration_name not in conn.applied


@pytest.mark.asyncio
async def test_reviewed_online_create_rejects_wrong_existing_identity_without_mutation(tmp_path):
    api = load_api_module()
    migration_name = "2026-08-18-03-messages-project-ts-hot-path-index.sql"
    source = REPO_ROOT / ".agents/data/migrations" / migration_name
    migration = tmp_path / migration_name
    shutil.copyfile(source, migration)
    protocol = api.REVIEWED_ONLINE_INDEX_MIGRATIONS[migration_name]
    conn = OnlineIndexConn(
        indexes={
            (protocol["schema"], protocol["index"]): {
                "table_schema": "public",
                "table_name": "messages",
                "valid": True,
                "ready": True,
                "definition": "CREATE INDEX idx_messages_project_ts ON public.messages (ts)",
            }
        }
    )

    with pytest.raises(HTTPException, match="mismatched catalog identity"):
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert conn.executed_migration_sql == []
    assert migration_name not in conn.applied
    assert [kind for kind, _key in conn.session_lock_calls] == ["lock", "unlock"]


@pytest.mark.asyncio
async def test_no_target_apply_skips_superseded_batch_and_executes_successor(tmp_path):
    api = load_api_module()
    superseded = write_migration(
        tmp_path,
        "2026-08-16-03-drop-degenerate-ivfflat-indexes.sql",
        "SELECT 'unsafe batch';\n",
    )
    successor = write_migration(
        tmp_path,
        "2026-08-17-04-drop-work-products-embedding-ivfflat.sql",
    )
    conn = MigrationConn(
        hnsw_replacements={
            ("idx_work_products_embedding_hnsw", "work_products"),
        }
    )

    result = await api.apply_schema_migrations(
        conn,
        dry_run=False,
        migration_dir=tmp_path,
    )

    actions = {row["id"]: row["action"] for row in result["results"]}
    assert len(result["results"]) == 2, "each migration must have exactly one result row"
    assert actions[superseded.name] == "skip_superseded"
    assert actions[successor.name] == "applied"
    assert superseded.name not in conn.applied
    assert successor.name in conn.applied
    assert conn.executed_migration_sql == [successor.read_text(encoding="utf-8")]


@pytest.mark.asyncio
async def test_direct_ivfflat_drop_requires_exact_live_hnsw_replacement(tmp_path):
    api = load_api_module()
    migration = write_migration(
        tmp_path,
        "2026-08-17-04-drop-work-products-embedding-ivfflat.sql",
    )
    missing = MigrationConn()

    with pytest.raises(HTTPException, match="exact ready HNSW replacement") as exc:
        await api.apply_schema_migrations(
            missing,
            dry_run=False,
            migration_dir=tmp_path,
            target_ids=[migration.name],
        )

    assert exc.value.status_code == 409
    assert missing.executed_migration_sql == []
    assert migration.name not in missing.applied

    ready = MigrationConn(
        hnsw_replacements={
            ("idx_work_products_embedding_hnsw", "work_products"),
        }
    )
    result = await api.apply_schema_migrations(
        ready,
        dry_run=False,
        migration_dir=tmp_path,
        target_ids=[migration.name],
    )

    assert result["applied_count"] == 1
    assert ready.executed_migration_sql == [migration.read_text(encoding="utf-8")]


@pytest.mark.asyncio
async def test_targeted_plan_ignores_unrelated_checksum_mismatch(tmp_path):
    api = load_api_module()
    unrelated = write_migration(tmp_path, "2026-06-01-unrelated.sql")
    target = write_migration(tmp_path, "2026-08-17-01-target.sql")
    conn = MigrationConn(
        {
            unrelated.name: applied_row(
                unrelated.name,
                unrelated,
                "wrong-but-unrelated-checksum",
            )
        }
    )

    result = await api.apply_schema_migrations(
        conn,
        dry_run=True,
        migration_dir=tmp_path,
        target_ids=[target.name],
    )

    assert [row["id"] for row in result["results"]] == [target.name]
    assert result["results"][0]["action"] == "would_apply"


@pytest.mark.asyncio
async def test_apply_schema_migrations_checksum_mismatch_blocks_apply(tmp_path):
    api = load_api_module()
    path = write_migration(tmp_path, "2026-06-01-alpha.sql")
    conn = MigrationConn(
        {
            "2026-06-01-alpha.sql": {
                "migration_id": "2026-06-01-alpha.sql",
                "checksum_sha256": "not-the-current-checksum",
                "source_path": str(path),
                "applied_by": "old-runner",
                "applied_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "statement_status": "SELECT 1",
                "surface_version": "old",
            }
        }
    )

    with pytest.raises(HTTPException) as exc:
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert exc.value.status_code == 409
    assert conn.executed_migration_sql == []


@pytest.mark.asyncio
async def test_ordered_renamed_migration_uses_legacy_ledger_id_when_checksum_matches(tmp_path):
    api = load_api_module()
    path = write_migration(tmp_path, "2026-06-15-identity-v2-1-foundation.sql")
    checksum = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    conn = MigrationConn(
        {
            "2026-06-15-identity-v2-foundation.sql": applied_row(
                "2026-06-15-identity-v2-foundation.sql",
                path,
                checksum,
            )
        }
    )

    result = await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert result["applied_count"] == 0
    assert result["results"][0]["action"] == "skip_applied"
    assert result["results"][0]["applied_migration_id"] == "2026-06-15-identity-v2-foundation.sql"
    assert conn.executed_migration_sql == []


@pytest.mark.asyncio
async def test_ordered_renamed_migration_legacy_checksum_mismatch_blocks_apply(tmp_path):
    api = load_api_module()
    path = write_migration(tmp_path, "2026-06-15-identity-v2-1-foundation.sql")
    conn = MigrationConn(
        {
            "2026-06-15-identity-v2-foundation.sql": applied_row(
                "2026-06-15-identity-v2-foundation.sql",
                path,
                "not-the-current-checksum",
            )
        }
    )

    with pytest.raises(HTTPException) as exc:
        await api.apply_schema_migrations(conn, dry_run=False, migration_dir=tmp_path)

    assert exc.value.status_code == 409
    assert conn.executed_migration_sql == []


def test_schema_migration_files_rejects_unsafe_filename(tmp_path):
    api = load_api_module()
    write_migration(tmp_path, "not-versioned.sql")

    with pytest.raises(HTTPException) as exc:
        api.schema_migration_files(tmp_path)

    assert exc.value.status_code == 500


def test_completion_handback_parent_is_protected_from_retention_delete():
    migration = (
        REPO_ROOT
        / ".agents/data/migrations/2026-07-26-handoff-completion-handback.sql"
    ).read_text(encoding="utf-8")
    retain = (REPO_ROOT / ".agents/scripts/cortex-retain").read_text(
        encoding="utf-8"
    )

    foreign_key = migration.split(
        "ADD CONSTRAINT handoffs_reply_to_handoff_id_fkey",
        1,
    )[1].split(";", 1)[0]
    assert "REFERENCES handoffs(id)" in foreign_key
    assert "ON DELETE CASCADE" not in foreign_key
    assert "status IN ('completed','abandoned','failed','archived')" in retain
    assert "child.reply_to_handoff_id = handoffs.id" in retain
    assert "child.status NOT IN ('completed','abandoned','failed','archived')" in retain


def test_ivfflat_successors_require_hnsw_and_drop_one_index_concurrently():
    migration_root = REPO_ROOT / ".agents/data/migrations"
    preflight = (
        migration_root
        / "2026-08-17-03-verify-vector-hnsw-replacements.sql"
    ).read_text(encoding="utf-8")
    expected_hnsw = {
        "idx_work_products_embedding_hnsw",
        "idx_lessons_embedding_hnsw",
        "idx_knowledge_embedding_hnsw",
        "idx_decisions_embedding_hnsw",
        "idx_messages_embedding_hnsw",
    }
    assert all(name in preflight for name in expected_hnsw)
    assert "meta.indisready" in preflight
    assert "meta.indisvalid" in preflight
    assert "access_method.amname = 'hnsw'" in preflight
    assert "indexed_column.attname = 'embedding'" in preflight
    assert "operator_class.opcname = 'vector_cosine_ops'" in preflight
    assert "meta.indpred IS NULL" in preflight

    drop_files = sorted(migration_root.glob("2026-08-17-0[4-8]-drop-*-ivfflat.sql"))
    assert len(drop_files) == 5
    for path in drop_files:
        sql = path.read_text(encoding="utf-8")
        statements = [part for part in sql.split(";") if part.strip()]
        assert len(statements) == 1
        assert "DROP INDEX CONCURRENTLY IF EXISTS public." in sql


def test_e2e4_upgrade_convergence_supplies_runtime_columns_and_online_index():
    migration_root = REPO_ROOT / ".agents/data/migrations"
    columns = (
        migration_root
        / "2026-08-17-01-e2e4-storage-columns-convergence.sql"
    ).read_text(encoding="utf-8")
    index = (
        migration_root
        / "2026-08-17-02-archive-messages-raw-session-index.sql"
    ).read_text(encoding="utf-8")

    assert "messages" in columns and "distilled boolean" in columns
    assert "archive_messages" in columns and "content_zstd bytea" in columns
    assert "raw_session_id uuid" in columns
    assert "decisions" in columns and "compacted boolean" in columns
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_archive_messages_raw_session" in index
