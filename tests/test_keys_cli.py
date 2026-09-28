"""Credential-bearing CLI behavior; never print test bearer values."""

from __future__ import annotations

import datetime
import io
import json
import secrets
import stat

import pytest

from cortex_v2.clients.client import CortexClient
from cortex_v2.clients.config import ClientProfile
from cortex_v2.clients.errors import ClientConfigError
from cortex_v2.clients.transport import HttpResponse


def profile(token: str, **kwargs) -> ClientProfile:
    return ClientProfile(
        base_url="http://127.0.0.1:8601",
        token=token,
        default_scope=None,
        default_read_scopes=(),
        installation_label="installation-1",
        principal_label="lead",
        source="test",
        **kwargs,
    )


def response(data: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, headers={}, body=json.dumps({"data": data}).encode())


def problem(code: str, status: int = 401) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={},
        body=json.dumps({"error": {"code": code, "message": "Credential changed", "retryable": False}}).encode(),
    )


@pytest.fixture(autouse=True)
def _select_linux_for_file_store_tests(monkeypatch, request):
    if request.node.name != "test_explicit_file_backend_denied_on_non_linux":
        from cortex_v2.clients import key_store
        monkeypatch.setattr(key_store, "_is_linux", lambda: True, raising=False)


def test_credential_objects_redact_repr():
    from cortex_v2.clients.key_store import KeyMetadata, _KeyRecord
    from cortex_v2.mcp.protocol import McpCredentials

    token = secrets.token_urlsafe(32)
    representations = (
        repr(profile(token)),
        repr(_KeyRecord(token, KeyMetadata("user", "2027-03-27T00:00:00Z"))),
        repr(McpCredentials(token=token, scope="alpha")),
    )
    if any(token in value for value in representations):
        pytest.fail("credential-bearing object representation disclosed a secret")


def test_file_store_private_atomic_and_refuses_links_and_unsafe_modes(tmp_path):
    from cortex_v2.clients.key_store import KeyStore, KeyStoreError

    old_key = secrets.token_urlsafe(32)
    renewed_key = secrets.token_urlsafe(32)
    blocked_key = secrets.token_urlsafe(32)
    root = tmp_path / "keys"
    store = KeyStore("installation-1", root=root, backend="file")
    store.put("alpha", "lead", old_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    if store.get("alpha", "lead") != old_key:
        pytest.fail("file store did not return its issued credential")
    assert store.metadata("alpha", "lead").managed_by == "user"
    assert store.metadata("alpha", "lead").expires_at == "2027-03-27T00:00:00Z"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "installation-1").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "installation-1" / "alpha").stat().st_mode) == 0o700
    path = root / "installation-1" / "alpha" / "lead.key"
    store.put("alpha", "lead", renewed_key, managed_by="kos", expires_at="2027-03-28T00:00:00Z")
    if store.get("alpha", "lead") != renewed_key:
        pytest.fail("file store did not replace its issued credential")
    assert store.metadata("alpha", "lead").managed_by == "kos"
    path.chmod(0o644)
    with pytest.raises(KeyStoreError):
        store.get("alpha", "lead")
    with pytest.raises(KeyStoreError):
        store.put("alpha", "lead", blocked_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    path.unlink()
    path.symlink_to(tmp_path / "target")
    with pytest.raises(KeyStoreError):
        store.get("alpha", "lead")
    with pytest.raises(KeyStoreError):
        store.put("alpha", "lead", blocked_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")

def test_file_store_rejects_hardlinked_credential(tmp_path):
    from cortex_v2.clients.key_store import KeyStore, KeyStoreError

    issued_key = secrets.token_urlsafe(32)
    blocked_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    path = tmp_path / "keys" / "installation-1" / "alpha" / "lead.key"
    (tmp_path / "duplicate.key").hardlink_to(path)
    with pytest.raises(KeyStoreError):
        store.get("alpha", "lead")
    with pytest.raises(KeyStoreError):
        store.put("alpha", "lead", blocked_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")


def test_file_store_refuses_secret_without_api_expiry_metadata(tmp_path):
    from cortex_v2.clients.key_store import KeyStore, KeyStoreError

    issued_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    path = tmp_path / "keys" / "installation-1" / "alpha" / "lead.key"
    path.write_text(json.dumps({"token": issued_key, "managed_by": "user"}))
    with pytest.raises(KeyStoreError):
        store.get("alpha", "lead")



def test_file_store_rejects_symlinked_directory(tmp_path):
    from cortex_v2.clients.key_store import KeyStore, KeyStoreError

    (tmp_path / "real").mkdir(mode=0o700)
    (tmp_path / "keys").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(KeyStoreError):
        KeyStore("installation-1", root=tmp_path / "keys", backend="file").put(
            "alpha", "lead", secrets.token_urlsafe(32), managed_by="user", expires_at="2027-03-27T00:00:00Z"
        )


def test_explicit_file_backend_denied_on_non_linux(tmp_path, monkeypatch):
    from cortex_v2.clients import key_store

    monkeypatch.setattr(key_store, "_is_linux", lambda: False, raising=False)
    with pytest.raises(key_store.KeyStoreError):
        key_store.KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    assert not (tmp_path / "keys").exists()


def test_due_status_uses_database_expiry_at_thirty_day_boundary(tmp_path):
    from cortex_v2.clients.key_store import KeyStore

    now = datetime.datetime(2026, 9, 28, 12, tzinfo=datetime.timezone.utc)
    due_at = (now + datetime.timedelta(days=30)).isoformat()
    later_at = (now + datetime.timedelta(days=30, microseconds=1)).isoformat()
    expired_at = (now - datetime.timedelta(seconds=1)).isoformat()
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "due", secrets.token_urlsafe(32), managed_by="kos", expires_at=due_at)
    store.put("alpha", "later", secrets.token_urlsafe(32), managed_by="user", expires_at=later_at)
    store.put("beta", "expired", secrets.token_urlsafe(32), managed_by="openkai", expires_at=expired_at)

    assert [
        (key.project, key.name, key.managed_by, key.expires_at, key.state)
        for key in store.due(now=now)
    ] == [
        ("alpha", "due", "kos", due_at, "due"),
        ("beta", "expired", "openkai", expired_at, "expired"),
    ]


def test_status_fails_closed_on_symlinked_key_without_partial_output(tmp_path):
    from cortex_v2.cli.keys import human_main
    from cortex_v2.clients.key_store import KeyStore, KeyStoreError

    now = datetime.datetime(2026, 9, 28, 12, tzinfo=datetime.timezone.utc)
    issued_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at=now.isoformat())
    (tmp_path / "outside").write_text("not a credential")
    (tmp_path / "keys" / "installation-1" / "alpha" / "other.key").symlink_to(tmp_path / "outside")

    with pytest.raises(KeyStoreError):
        store.due(now=now)
    out, err = io.StringIO(), io.StringIO()
    code = human_main(
        ["status", "--installation", "installation-1"], store=store,
        out=out, err=err, now=now,
    )
    assert code == 2 and not out.getvalue()
    if issued_key in err.getvalue():
        pytest.fail("status error disclosed a credential")


def test_status_lists_only_due_local_keys_without_api_access(tmp_path):
    from cortex_v2.cli.keys import human_main
    from cortex_v2.clients.key_store import KeyStore

    now = datetime.datetime(2026, 9, 28, 12, tzinfo=datetime.timezone.utc)
    issued_key = secrets.token_urlsafe(32)
    later_key = secrets.token_urlsafe(32)
    expiry = (now + datetime.timedelta(days=30)).isoformat()
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at=expiry)
    store.put(
        "alpha", "later", later_key, managed_by="kos",
        expires_at=(now + datetime.timedelta(days=31)).isoformat(),
    )
    out, err = io.StringIO(), io.StringIO()
    code = human_main(
        ["status", "--installation", "installation-1"], store=store,
        out=out, err=err, now=now,
    )
    assert code == 0 and not err.getvalue()
    assert json.loads(out.getvalue()) == {
        "project": "alpha", "name": "lead", "managed_by": "user",
        "expires_at": expiry, "state": "due",
    }
    if issued_key in out.getvalue() or later_key in out.getvalue():
        pytest.fail("status disclosed a credential")


def test_profile_uses_store_or_explicit_ci_key_only(tmp_path):
    from cortex_v2.clients.config import load_client_profile
    from cortex_v2.clients.key_store import KeyStore

    issued_key = secrets.token_urlsafe(32)
    ci_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    resolved = load_client_profile(
        env={"CORTEX_URL": "http://127.0.0.1:8601"},
        store=store, project="alpha", name="lead",
    )
    if resolved.token != issued_key:
        pytest.fail("selected file-store credential was not loaded")
    with pytest.raises(ClientConfigError):
        load_client_profile(env={"CORTEX_URL": "http://127.0.0.1:8601", "CORTEX_KEY": issued_key})
    with pytest.raises(ClientConfigError):
        load_client_profile(env={"CORTEX_V2_TOKEN": issued_key, "CORTEX_URL": "http://127.0.0.1:8601"}, store=store, project="alpha", name="lead")
    explicit = load_client_profile(env={"CORTEX_URL": "http://127.0.0.1:8601", "CI": "true", "CORTEX_KEY": ci_key})
    if explicit.token != ci_key:
        pytest.fail("explicit CI credential was not selected")
    with pytest.raises(ClientConfigError):
        load_client_profile(
            env={"CORTEX_URL": "http://127.0.0.1:8601", "CI": "true", "CORTEX_KEY": ""},
            store=store, project="alpha", name="lead",
        )


def test_token_bearing_connection_file_is_rejected(tmp_path):
    from cortex_v2.clients.config import load_client_profile
    from cortex_v2.clients.key_store import KeyStore

    issued_key = secrets.token_urlsafe(32)
    path = tmp_path / "connection.json"
    path.write_text(json.dumps({
        "profile": "v2", "base_url": "http://127.0.0.1:8601",
        "installation": "installation-1", "project": "alpha", "name": "lead",
        "token": issued_key,
    }))
    path.chmod(0o600)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    with pytest.raises(ClientConfigError) as denied:
        load_client_profile(path, env={}, store=store)
    if issued_key in str(denied.value):
        pytest.fail("configuration error disclosed the rejected credential")


def test_nonloopback_url_requires_tls(tmp_path):
    from cortex_v2.clients.config import load_client_profile

    with pytest.raises(ClientConfigError):
        load_client_profile(env={"CORTEX_URL": "http://remote.example:8601", "CI": "true", "CORTEX_KEY": secrets.token_urlsafe(32)})


def test_mcp_stdio_reads_store_but_http_keeps_caller_bearer_only(tmp_path):
    from cortex_v2.clients.config import load_client_profile
    from cortex_v2.clients.key_store import KeyStore
    from cortex_v2.mcp.protocol import McpCredentials
    from cortex_v2.mcp.server import stdio_client_factory

    issued_key = secrets.token_urlsafe(32)
    request_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    profile = load_client_profile(env={"CORTEX_URL": "http://127.0.0.1:8601"}, store=store, project="alpha", name="lead")
    stdio_client = stdio_client_factory(profile)(McpCredentials(token=None, scope="alpha"))
    if stdio_client.profile.token != issued_key:
        pytest.fail("stdio MCP did not select its private credential")
    assert stdio_client.profile.credential_store is store
    from cortex_v2.clients.config import profile_from_credentials
    http_profile = profile_from_credentials("http://127.0.0.1:8601", request_key)
    if http_profile.token != request_key or http_profile.credential_store is not None:
        pytest.fail("HTTP MCP did not preserve caller-only credentials")

def test_http_mcp_never_substitutes_local_identity(tmp_path):
    from fastapi.testclient import TestClient

    from cortex_v2.clients.key_store import KeyStore
    from cortex_v2.mcp.server import create_mcp_app

    issued_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "owner", issued_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    requests = []
    with TestClient(create_mcp_app(client_factory=lambda credentials: requests.append(credentials))) as http:
        response = http.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "protocol_descriptor", "arguments": {}},
        })
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credential"
    assert requests == []



def test_kos_and_openkai_select_distinct_private_credentials(tmp_path, monkeypatch):
    from cortex_v2.clients.adapters.kos import KosAdapter
    from cortex_v2.clients.adapters.openkai import OpenKaiMemoryAdapter
    from cortex_v2.clients.key_store import KeyStore

    kos_key = secrets.token_urlsafe(32)
    kai_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "kos", kos_key, managed_by="kos", expires_at="2027-03-27T00:00:00Z")
    store.put("alpha", "kai", kai_key, managed_by="openkai", expires_at="2027-03-27T00:00:00Z")
    monkeypatch.setenv("CORTEX_URL", "http://127.0.0.1:8601")
    kos = KosAdapter(store=store, project="alpha", name="kos")
    openkai = OpenKaiMemoryAdapter("cortex", store=store, scope="alpha", name="kai")
    auth_headers = []

    def transport(method, url, *, headers, **kwargs):
        auth_headers.append(headers["Authorization"])
        return response({"entries": [], "ok": True})

    kos._client._transport = transport
    openkai._client._transport = transport
    kos.poll_feed(scope="alpha")
    assert openkai.recall("question").state == "ok"
    if auth_headers != [f"Bearer {kos_key}", f"Bearer {kai_key}"]:
        pytest.fail("KOS and OpenKai crossed private identities")


def test_human_whoami_prints_rights_not_bearer(tmp_path):
    from cortex_v2.cli.keys import human_main
    from cortex_v2.clients.key_store import KeyStore

    issued_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    calls = []

    def transport(method, url, *, headers, query=None, **kwargs):
        calls.append((method, url, headers, query))
        return response({
            "installation_id": "installation-1", "project": "alpha", "name": "lead",
            "role": "lead", "principal_id": "principal-1", "scopes": [],
            "rights": {"read": True, "write": True, "manage_keys": True, "create_projects": False},
        })

    client = CortexClient(profile(issued_key), transport=transport)
    out, err = io.StringIO(), io.StringIO()
    code = human_main(["whoami", "--project", "alpha", "--name", "lead"], client=client, store=store, out=out, err=err)
    assert code == 0
    assert "lead" in out.getvalue() and "create_projects" in out.getvalue()
    if issued_key in out.getvalue() + err.getvalue():
        pytest.fail("whoami disclosed its bearer")
    assert calls[0][0:2] == ("GET", "http://127.0.0.1:8601/v1/auth/principal")
    assert calls[0][3] == {"project": "alpha"}


def test_whoami_refuses_wrong_project_even_on_http_200(tmp_path):
    from cortex_v2.cli.keys import human_main
    from cortex_v2.clients.key_store import KeyStore

    issued_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    client = CortexClient(
        profile(issued_key),
        transport=lambda *args, **kwargs: response({
            "installation_id": "installation-1", "project": "other", "name": "lead",
            "role": "lead", "principal_id": "principal-1", "scopes": [],
            "rights": {"read": True, "write": True, "manage_keys": True, "create_projects": True},
        }),
    )
    out, err = io.StringIO(), io.StringIO()
    code = human_main(
        ["whoami", "--project", "alpha", "--name", "lead"],
        client=client, store=store, out=out, err=err,
    )
    assert code != 0 and out.getvalue() == ""
    if issued_key in err.getvalue():
        pytest.fail("whoami error disclosed its bearer")


def test_whoami_rejects_invalid_bearer_without_retry(tmp_path):
    from cortex_v2.cli.keys import human_main
    from cortex_v2.clients.key_store import KeyStore

    issued_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    store.put("alpha", "lead", issued_key, managed_by="user", expires_at="2027-03-27T00:00:00Z")
    requests = []

    def transport(method, url, **kwargs):
        requests.append(url)
        return problem("invalid_credential")

    client = CortexClient(profile(issued_key), transport=transport)
    out, err = io.StringIO(), io.StringIO()
    code = human_main(["whoami", "--project", "alpha", "--name", "lead"], client=client, store=store, out=out, err=err)
    assert code != 0 and len(requests) == 1
    assert not out.getvalue()
    if issued_key in err.getvalue():
        pytest.fail("invalid-credential error disclosed its bearer")


def test_whoami_warns_once_for_due_or_expired_stored_key_not_ci(tmp_path):
    from cortex_v2.cli.keys import human_main
    from cortex_v2.clients.key_store import KeyStore

    now = datetime.datetime(2026, 9, 28, 12, tzinfo=datetime.timezone.utc)
    issued_key = secrets.token_urlsafe(32)
    store = KeyStore("installation-1", root=tmp_path / "keys", backend="file")
    identity = {
        "installation_id": "installation-1", "project": "alpha", "name": "lead",
        "role": "lead", "principal_id": "principal-1", "scopes": [],
        "rights": {"read": True, "write": True, "manage_keys": True, "create_projects": False},
    }
    transport = lambda *args, **kwargs: response(identity)
    local = CortexClient(
        profile(issued_key, credential_store=store, credential_project="alpha", credential_name="lead"),
        transport=transport,
    )
    for remaining, classification in (
        (datetime.timedelta(days=31), None),
        (datetime.timedelta(days=30), "due"),
        (datetime.timedelta(0), "expired"),
    ):
        expiry = (now + remaining).isoformat()
        store.put("alpha", "lead", issued_key, managed_by="user", expires_at=expiry)
        out, err = io.StringIO(), io.StringIO()
        code = human_main(
            ["whoami", "--project", "alpha", "--name", "lead"],
            client=local, out=out, err=err, now=now,
        )
        assert code == 0
        if classification is None:
            assert not err.getvalue()
        else:
            assert err.getvalue().count("Warning:") == 1
            assert classification in err.getvalue() and expiry in err.getvalue()
        if issued_key in out.getvalue() + err.getvalue():
            pytest.fail("whoami warning disclosed a credential")

    ci = CortexClient(profile(issued_key), transport=transport)
    out, err = io.StringIO(), io.StringIO()
    assert human_main(
        ["whoami", "--project", "alpha", "--name", "lead"],
        client=ci, out=out, err=err, now=now,
    ) == 0
    assert not err.getvalue()
