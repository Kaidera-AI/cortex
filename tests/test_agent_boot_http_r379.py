"""Actual mounted own-member GET; repository/auth/operational rows are declared seams."""
import copy
import importlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cortex_v2.store import ApiProblem, Principal, Scope, ScopeContext
from boot_r374_fixture import CASES, native_expected, required, seed, uid


@pytest.mark.parametrize("agent,variant,options", CASES)
def test_mounted_native_boot_is_exact_and_read_only_under_its_own_scope(agent, variant, options, monkeypatch):
    module = required()
    assert callable(getattr(module, "read_boot", None)) and callable(getattr(module, "load_boot_snapshot", None)), "R379 native boot reader missing"
    snapshot, original = seed(agent, variant)
    app_module = importlib.import_module("cortex_v2.app")
    pid = uid("principal-" + agent)
    scope = Scope("helix", snapshot["context"]["project_scope_id"], "project", True, False, False)
    context = ScopeContext(Principal(pid, snapshot["context"]["installation_id"]), scope, (scope,))
    effects, loads = [], []

    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class Connection:
        def transaction(self): return Transaction()
        async def execute(self, sql, *args):
            assert "set_config" in sql.lower(), "GET boot attempted a write outside read-scope setup"
            effects.append((sql, args))

    connection = Connection()

    class Acquisition:
        async def __aenter__(self): return connection
        async def __aexit__(self, *a): pass

    async def auth(*a, **k): return context.principal
    async def scopes(conn, principal, alias, read_scopes, *, write):
        assert conn is connection and principal is context.principal
        assert alias == "helix" and read_scopes == ["helix"] and write is False
        return context
    async def load(conn, ctx, *args, **kwargs):
        assert conn is connection and ctx is context
        loads.append((args, kwargs))
        return copy.deepcopy(snapshot)
    monkeypatch.setattr(app_module, "authenticate", auth)
    monkeypatch.setattr(app_module, "resolve_scopes", scopes)
    monkeypatch.setattr(module, "load_boot_snapshot", load)
    app = app_module.create_app()
    app.state.pool = SimpleNamespace(acquire=lambda: Acquisition())
    app.state.profile = SimpleNamespace(instance_id="PUBLIC r379 app fixture")
    app.state.settings = SimpleNamespace(token_pepper=b"PUBLIC unissued fixture pepper")
    params = {"budget": options[options.index("--budget") + 1] if "--budget" in options else "1200"}
    if "--full" in options: params["full"] = "true"
    if "--query" in options: params["query"] = "release"
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boot/" + agent, params=params, headers={
        "Authorization": "Bearer " + "A" * 43, "X-Cortex-Scope": "helix", "X-Agent-Name": agent})
    assert response.status_code == 200 and response.json() == native_expected(snapshot, original)
    assert len(loads) == 1
    assert all("set_config" in sql.lower() for sql, _ in effects)
    assert "Bearer" not in response.text and "Traceback" not in response.text


@pytest.mark.parametrize("case", ("other-agent", "scope-mismatch", "header-mismatch", "inactive-actor"))
def test_reader_never_borrows_identity_from_url_header_scope_or_retired_binding(case, monkeypatch):
    module = required()
    assert callable(getattr(module, "read_boot", None)) and callable(getattr(module, "load_boot_snapshot", None)), "R379 native boot reader missing"
    snapshot, _ = seed()
    pid = uid("principal-kai")
    scope = Scope("helix", snapshot["context"]["project_scope_id"], "project", True, False, False)
    context = ScopeContext(Principal(pid, snapshot["context"]["installation_id"]), scope, (scope,))
    if case == "scope-mismatch": snapshot["context"]["project_scope_id"] = uid("foreign-scope")
    if case == "inactive-actor":
        retired = copy.deepcopy(snapshot["agent_rows"][0])
        retired.update(revision=2, state="retired")
        snapshot["agent_rows"].append(retired)
    async def load(*a, **k): return copy.deepcopy(snapshot)
    monkeypatch.setattr(module, "load_boot_snapshot", load)
    with pytest.raises((ApiProblem, ValueError)):
        # This boundary accepts both URL and optional header labels, neither of
        # which may replace the credential-derived source context.
        import asyncio
        asyncio.run(module.read_boot(SimpleNamespace(), context,
            "bob" if case == "other-agent" else "kai", budget=1200, query=None, full=False,
            agent_label="bob" if case == "header-mismatch" else "kai"))
