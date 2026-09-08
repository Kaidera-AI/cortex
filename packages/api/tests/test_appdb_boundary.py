"""The standalone API cannot discover or mutate a KOS-owned app database."""

import ast
import importlib.util
import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


MAIN = Path(__file__).resolve().parents[1] / "main.py"
REMOVED_SYMBOLS = (
    "HARNESS_APPDB_DSN_DEFAULT", "HARNESS_APPDB_CONNECT_TIMEOUT",
    "CONSOLE_APPDB_PROJECT_TABLE_CONFLICT_KEYS", "CONSOLE_APPDB_PROJECT_JSON_COLUMNS",
    "harness_appdb_dsn", "update_console_appdb_project_table",
    "update_console_appdb_default_project", "update_console_appdb_project_json",
    "migrate_console_appdb_project_key",
)


def load_api():
    spec = importlib.util.spec_from_file_location("cortex_api_appdb_boundary_test", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def poison_appdb_environment(monkeypatch):
    for key in ("HARNESS_APPDB_DSN_HOST", "HARNESS_APPDB_DSN", "HARNESS_APPDB_MIGRATION_TIMEOUT"):
        monkeypatch.setenv(key, "appdb-test-value-must-never-be-read")
    original = os._Environ.__getitem__

    def guarded(environ, key):
        __tracebackhide__ = True  # Never render the process environment in a failure.
        if str(key).startswith("HARNESS_APPDB"):
            raise AssertionError("Cortex read KOS app-DB configuration")
        return original(environ, key)

    monkeypatch.setattr(os._Environ, "__getitem__", guarded)


@pytest.mark.parametrize("symbol", REMOVED_SYMBOLS)
def test_appdb_symbols_are_absent_from_api(symbol):
    module = load_api()
    assert not hasattr(module, symbol)


def test_api_import_does_not_read_poisoned_appdb_configuration(monkeypatch):
    poison_appdb_environment(monkeypatch)
    load_api()


def test_project_key_primitive_has_only_cortex_parameters():
    assert tuple(inspect.signature(load_api().migrate_project_key).parameters) == (
        "conn", "old_key", "new_key",
    )


def test_registration_has_no_dormant_transfer_hook_and_returns_null():
    tree = ast.parse(MAIN.read_text())
    route = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "register_project")
    assert not any(isinstance(n, ast.Name) and n.id == "migration_result" for n in ast.walk(route))
    values = [value for n in ast.walk(route) if isinstance(n, ast.Dict)
              for key, value in zip(n.keys, n.values)
              if isinstance(key, ast.Constant) and key.value == "migrated_from_project_key"]
    assert len(values) == 1
    assert isinstance(values[0], ast.Constant) and values[0].value is None


@pytest.mark.asyncio
async def test_held_project_transfer_refuses_before_any_storage(monkeypatch):
    api = load_api()
    poison_appdb_environment(monkeypatch)
    monkeypatch.setattr(api, "require_admin_access", lambda _request: None)

    class NoPool:
        def acquire(self):
            raise AssertionError("held transfer must not acquire storage")

    api.pool_admin = NoPool()
    body = api.ProjectRegister(
        project_key="appdb-boundary", repo_root="/projects/appdb-boundary",
        metadata={"allow_project_key_migration": True},
    )
    with pytest.raises(HTTPException) as caught:
        await api.register_project(body, SimpleNamespace(headers={}))
    assert caught.value.status_code == 409
    assert "explicit migration workflow" in caught.value.detail


@pytest.mark.asyncio
async def test_noop_project_key_primitive_keeps_existing_cortex_result(monkeypatch):
    api = load_api()
    poison_appdb_environment(monkeypatch)
    result = await api.migrate_project_key(object(), old_key="same", new_key="same")
    assert result == {"migrated": False, "old_key": "same", "new_key": "same", "counts": {}}
