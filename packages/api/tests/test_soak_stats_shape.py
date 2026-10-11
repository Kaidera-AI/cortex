"""Offline source contract: execute the actual API stats response builder.

Only its database/auth prerequisites are fixtures. The endpoint body and CLI
scan extractor are the repository's real code; no native soak is claimed.
"""
import ast
import asyncio
from datetime import datetime, timezone
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

from test_soak_completion_source import source_case


API = Path(__file__).resolve().parents[1] / "main.py"
SOAK = Path(__file__).resolve().parents[2] / "cli" / "cortex-search-soak"


def load_soak():
    loader = SourceFileLoader("soak_real_stats_shape", str(SOAK))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def real_stats(*, knowledge=4, decisions=9, empty=False):
    """Execute admin_stats unchanged against explicit bounded DB replies."""
    tree = ast.parse(API.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "admin_stats")
    function.decorator_list = []

    class Connection:
        async def fetchval(self, sql):
            if "pg_database_size" in sql:
                return 2097152
            if "count(*)" in sql:
                return 12
            if "stats_reset" in sql:
                return datetime(2026, 10, 11, tzinfo=timezone.utc)
            raise AssertionError("unhandled actual stats scalar query")

        async def fetch(self, sql, *args):
            if "n.nspname AS schema" in sql:
                assert len(args) == 1 and "knowledge" in args[0]
                return [] if empty else [
                    {"schema": "public", "rel": "knowledge", "kind": "r", "oid": 101,
                     "total": 1048576, "heap": 524288},
                    {"schema": "cortex", "rel": "decisions", "kind": "r", "oid": 102,
                     "total": 1048576, "heap": 524288},
                ]
            if "FROM pg_index" in sql:
                assert args[0] in (101, 102)
                name, scans = (("idx_knowledge_content_trgm", knowledge) if args[0] == 101
                               else ("idx_decisions_summary_trgm", decisions))
                return [{"name": name, "size": 65536, "def": "generated index fixture", "scans": scans},
                        {"name": "ordinary_index", "size": 16384, "def": "generated index fixture", "scans": 77}]
            raise AssertionError("unhandled actual stats relation query")

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    namespace = {"Request": Any, "Any": Any, "pool_admin": Pool(),
                 "require_admin_access": lambda _: None,
                 "quote_ident": lambda value: '"' + value.replace('"', '""') + '"'}
    body = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(body, str(API), "exec"), namespace)
    return asyncio.run(namespace["admin_stats"](object()))


def test_actual_api_objects_expose_trigram_scan_counts():
    stats = real_stats()
    assert set(stats["objects"]) == {"public.knowledge", "cortex.decisions"}
    assert "tables" not in stats
    assert load_soak().trgm_scans(stats) == {
        "idx_knowledge_content_trgm": 4, "idx_decisions_summary_trgm": 9,
    }, "CLI ignored the actual API objects/indexes/scans response"


def test_actual_api_zero_scans_remain_visible():
    assert load_soak().trgm_scans(real_stats(knowledge=0, decisions=0)) == {
        "idx_knowledge_content_trgm": 0, "idx_decisions_summary_trgm": 0,
    }, "zero scan counters disappeared instead of reaching the no-growth guard"


def shape_case(monkeypatch, before, after):
    actual_scans = load_soak().trgm_scans
    module, _ = source_case(monkeypatch)
    responses = iter([before, after])
    monkeypatch.setattr(module, "admin_stats", lambda *_: next(responses))
    monkeypatch.setattr(module, "trgm_scans", actual_scans)
    return module


def test_actual_shape_growth_allows_completed_source_soak(monkeypatch, capsys):
    module = shape_case(monkeypatch, real_stats(), real_stats(knowledge=8, decisions=12))
    assert module.main() == 0, "actual API scan growth was treated as missing evidence"
    assert "GATE PASSED" in capsys.readouterr().out


def test_actual_shape_no_growth_still_fails_its_existing_guard(monkeypatch, capsys):
    module = shape_case(monkeypatch, real_stats(), real_stats())
    assert module.main() == 1, "known unchanged counters were treated as missing evidence"
    output = capsys.readouterr().out
    assert "no trigram index scan growth" in output and "GATE PASSED" not in output


def test_actual_empty_stats_still_refuse_missing_evidence(monkeypatch, capsys):
    module = shape_case(monkeypatch, real_stats(empty=True), real_stats(empty=True))
    assert module.main() == 2
    output = capsys.readouterr().out
    assert "trigram index counters were unavailable" in output and "GATE PASSED" not in output


def test_absent_stats_still_produce_no_counters():
    assert load_soak().trgm_scans(None) == {}
    assert load_soak().trgm_scans({}) == {}
