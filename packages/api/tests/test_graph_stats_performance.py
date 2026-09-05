import importlib.util
import re
import sys
from pathlib import Path

import pytest


API_MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "data" / "cortex-schema-full.sql"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    api_root = str(path.parent)
    sys.path.insert(0, api_root)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(api_root)
    return module


class StatsConnection:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        offset = 0 if args == ("marketing",) else 100
        return {
            "entity_count": 1 + offset,
            "relationship_count": 2 + offset,
            "decision_count": 3 + offset,
            "lesson_count": 4 + offset,
            "knowledge_count": 5 + offset,
            "work_product_count": 6 + offset,
            "decision_backlog": 7 + offset,
            "lesson_backlog": 8 + offset,
            "knowledge_backlog": 9 + offset,
            "work_product_backlog": 10 + offset,
        }


@pytest.mark.asyncio
async def test_graph_stats_scans_each_project_relation_once():
    module = load_module(API_MAIN_PATH, "cortex_api_graph_stats_scan_test")
    conn = StatsConnection()

    result = await module.graph_stats(conn, "marketing")

    assert result == {
        "entity_count": 1,
        "relationship_count": 2,
        "source_counts": {
            "decisions": 3,
            "lessons": 4,
            "knowledge": 5,
            "work_products": 6,
        },
        "backlog": {
            "decisions": 7,
            "lessons": 8,
            "knowledge": 9,
            "work_products": 10,
        },
    }
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert args == ("marketing",)
    for table in (
        "cortex_entities",
        "cortex_relationships",
        "decisions",
        "lessons",
        "knowledge",
        "work_products",
    ):
        assert len(re.findall(rf"\bFROM\s+{table}\b", sql, re.IGNORECASE)) == 1
    assert len(re.findall(r"\bproject\s*=\s*\$1\b", sql)) == 6
    assert sql.count("COUNT(*) FILTER") == 4


@pytest.mark.asyncio
async def test_graph_stats_keeps_results_isolated_by_project_parameter():
    module = load_module(API_MAIN_PATH, "cortex_api_graph_stats_scope_test")
    conn = StatsConnection()

    marketing = await module.graph_stats(conn, "marketing")
    sales = await module.graph_stats(conn, "sales")

    assert marketing["entity_count"] == 1
    assert sales["entity_count"] == 101
    assert marketing["backlog"]["work_products"] == 10
    assert sales["backlog"]["work_products"] == 110
    assert [args for _sql, args in conn.calls] == [("marketing",), ("sales",)]


def test_graph_stats_relations_have_project_index_contracts():
    schema = SCHEMA_PATH.read_text()
    expected_indexes = {
        "cortex_entities": "idx_cortex_entities_project",
        "cortex_relationships": "idx_cortex_relationships_project",
        "decisions": "idx_decisions_project",
        "lessons": "idx_lessons_project",
        "knowledge": "idx_knowledge_project",
        "work_products": "idx_work_products_project_status",
    }

    for table, index in expected_indexes.items():
        assert index in schema
        assert re.search(
            rf"CREATE (?:UNIQUE )?INDEX {index} ON public\.{table} .*\(project(?:[,)]|\s)",
            schema,
        )
