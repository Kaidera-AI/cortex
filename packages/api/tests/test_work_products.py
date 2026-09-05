import importlib.util
from pathlib import Path

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_work_product_memory_text_is_domain_agnostic_and_hash_stable():
    module = load_module(API_MAIN_PATH, "cortex_api_work_products_test")
    payload = {
        "activity_type": "approved-post",
        "status": "current",
        "title": "Approved launch post",
        "summary": "Final LinkedIn launch copy approved and scheduled.",
        "behavior_summary": "Future agents should reuse the approved copy instead of rewriting it.",
        "architecture_notes": "Stored as project work-product memory, not code-only memory.",
        "files_changed": ["docs/posts/launch.md"],
        "symbols_changed": [],
        "subject_entities": ["LinkedIn launch", "content approval"],
        "artifact_refs": ["artifact://launch-visual"],
        "tests_run": [{"command": "brand review", "result": "passed"}],
        "risks": ["Do not publish stale draft copy."],
        "followups": ["Record platform post URL after publish."],
    }

    text = module.work_product_memory_text(payload)

    assert "Approved launch post" in text
    assert "LinkedIn launch" in text
    assert "brand review passed" in text
    assert "docs/posts/launch.md" in text
    assert module.work_product_content_hash(payload) == module.work_product_content_hash(dict(payload))


def test_work_product_status_validation_is_fail_loud():
    module = load_module(API_MAIN_PATH, "cortex_api_work_product_status_test")

    assert module.normalize_work_product_status("current") == "current"
    try:
        module.normalize_work_product_status("unknown")
    except module.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("invalid work-product status should raise")


def test_work_product_file_hashes_are_best_effort_and_stable(tmp_path):
    module = load_module(API_MAIN_PATH, "cortex_api_work_product_hashes_test")
    tracked = tmp_path / "docs" / "decision.md"
    tracked.parent.mkdir()
    tracked.write_text("approved architecture note\n", encoding="utf-8")

    hashes = module.compute_file_hashes(["docs/decision.md", "missing.md"], root=tmp_path)

    assert list(hashes) == ["docs/decision.md"]
    assert len(hashes["docs/decision.md"]) == 64
    assert hashes == module.compute_file_hashes(["docs/decision.md"], root=tmp_path)


def test_work_product_graph_extract_links_receipt_to_files_and_subjects():
    module = load_module(API_MAIN_PATH, "cortex_api_work_product_graph_test")

    entities, relationships = module.work_product_graph_extract(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "title": "Work Product Memory hardening",
            "summary": "Cortex now records projection and freshness metadata.",
            "activity_type": "task-completed",
            "status": "current",
            "agent_name": "kai@kaidera-os",
            "files_changed": [".agents/api/main.py"],
            "symbols_changed": ["beat_work_products_check_freshness"],
            "subject_entities": ["Work Product Memory", "Knowledge graph"],
            "artifact_refs": [],
            "tests_run": [],
            "risks": [],
            "followups": [],
        }
    )

    entity_keys = {(item["name"], item["type"]) for item in entities}
    rel_keys = {(item["source"], item["type"], item["target"]) for item in relationships}

    assert ("Work Product Memory hardening", "work_product") in entity_keys
    assert (".agents/api/main.py", "file") in entity_keys
    assert ("kai", "agent") in entity_keys
    assert (
        "Work Product Memory hardening",
        "modifies",
        ".agents/api/main.py",
    ) in rel_keys
    assert (
        "Work Product Memory hardening",
        "documents",
        "Knowledge graph",
    ) in rel_keys


@pytest.mark.asyncio
async def test_projection_status_snapshot_summarizes_existing_projection_surfaces():
    module = load_module(API_MAIN_PATH, "cortex_api_projection_status_test")

    class ProjectionConn:
        async def execute(self, sql, *args):
            return "OK"

        async def fetchval(self, sql, *args):
            if "to_regclass('public.cortex_entities')" in sql:
                return True
            if "to_regclass('public.work_products')" in sql:
                return True
            raise AssertionError(f"Unexpected fetchval SQL: {sql!r}")

        async def fetchrow(self, sql, *args):
            if "entity_count" in sql and "relationship_count" in sql:
                return {
                    "entity_count": 7,
                    "relationship_count": 5,
                    "decision_count": 3,
                    "lesson_count": 1,
                    "knowledge_count": 2,
                    "work_product_count": 1,
                    "decision_backlog": 1,
                    "lesson_backlog": 2,
                    "knowledge_backlog": 0,
                    "work_product_backlog": 3,
                }
            raise AssertionError(f"Unexpected fetchrow SQL: {sql!r}")

        async def fetch(self, sql, *args):
            if "information_schema.columns" in sql and "table_name = 'work_products'" in sql:
                return []
            if "GROUP BY COALESCE(projection_status" in sql:
                return [
                    {"projection_status": "projected", "freshness_status": "current", "count": 4},
                    {"projection_status": "pending", "freshness_status": "unknown", "count": 2},
                ]
            if "FROM work_products" in sql and "projection_error" in sql:
                return [
                    {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "title": "Pending graph projection",
                        "projection_status": "pending",
                        "projection_error": "",
                        "freshness_status": "unknown",
                        "freshness_reason": "file_hashes_unavailable",
                        "updated_at": "2026-06-24T10:00:00+00:00",
                    }
                ]
            if "FROM embedding_backfill_jobs" in sql and "GROUP BY status" in sql:
                return [
                    {"status": "queued", "count": 1},
                    {"status": "running", "count": 1},
                ]
            if "FROM embedding_backfill_jobs" in sql:
                return [
                    {
                        "id": "22222222-2222-4222-8222-222222222222",
                        "table_name": "work_products",
                        "status": "running",
                        "processed": 10,
                        "embedded": 9,
                        "errors": 1,
                        "skipped": 0,
                        "created_at": "2026-06-24T09:00:00+00:00",
                        "updated_at": "2026-06-24T10:00:00+00:00",
                    }
                ]
            if "FROM graph_build_jobs" in sql and "GROUP BY status" in sql:
                return [
                    {"status": "queued", "count": 1},
                    {"status": "completed", "count": 2},
                ]
            if "FROM graph_build_jobs" in sql:
                return [
                    {
                        "id": "33333333-3333-4333-8333-333333333333",
                        "repo": "kaidera-os",
                        "status": "completed",
                        "full": True,
                        "embed": True,
                        "error": "",
                        "created_at": "2026-06-24T08:00:00+00:00",
                        "updated_at": "2026-06-24T08:30:00+00:00",
                    }
                ]
            raise AssertionError(f"Unexpected fetch SQL: {sql!r}")

    # The job-ledger ensure_* helpers acquire pool_admin themselves: cortex_app has
    # USAGE but not CREATE on schema public, so DDL cannot ride the scoped connection.
    class _Acquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _AdminPool:
        def acquire(self):
            return _Acquire(ProjectionConn())

    module.pool_admin = _AdminPool()

    snapshot = await module.projection_status_snapshot(ProjectionConn(), "project-a")

    assert snapshot["project"] == "project-a"
    assert snapshot["graph"]["total_backlog"] == 6
    assert snapshot["work_products"]["projection_status"] == {"projected": 4, "pending": 2}
    assert snapshot["work_products"]["freshness_status"] == {"current": 4, "unknown": 2}
    assert snapshot["work_products"]["attention"][0]["projection_status"] == "pending"
    assert snapshot["embedding_jobs"]["status"] == {"queued": 1, "running": 1}
    assert snapshot["embedding_jobs"]["recent"][0]["processed"] == 10
    assert snapshot["graph_build_jobs"]["status"] == {"queued": 1, "completed": 2}
    assert snapshot["graph_build_jobs"]["recent"][0]["repo"] == "kaidera-os"
    assert snapshot["boot"]["metadata_path"] == "persona.metadata.boot_context"
