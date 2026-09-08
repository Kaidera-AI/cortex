"""Real OpenSSL custody and in-memory TLS handshake effects; no live database."""

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import ssl
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("db_tls", ROOT / ".agents/api/db_tls.py")
db_tls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db_tls)
INIT = ROOT / "local-cortex/standalone/tls/init.sh"
APP_DSN = "postgresql://cortex_app@cortex-pg:5432/cortex"
ADMIN_DSN = "postgresql://postgres@cortex-pg:5432/cortex"


def initialize(root, action="init"):
    env = {
        **os.environ,
        "CORTEX_TLS_ROOT": str(root),
        "CORTEX_TLS_SERVER_UID": str(os.getuid()),
        "CORTEX_TLS_CLIENT_UID": str(os.getuid()),
        "CORTEX_TLS_SERVER_GID": str(os.getgid()),
        "CORTEX_TLS_CLIENT_GID": str(os.getgid()),
    }
    return subprocess.run(["sh", str(INIT), action], env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module")
def fresh_pki(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL executable is required for PKI effects")
    root = tmp_path_factory.mktemp("cortex-pki")
    result = initialize(root)
    assert result.returncode == 0, result.stderr
    return root


@pytest.fixture
def pki(fresh_pki, tmp_path, monkeypatch):
    root = tmp_path / "tls"
    shutil.copytree(fresh_pki, root)
    # copytree preserves bytes/mode/mtime, but copies inherit the temp parent's GID.
    before = fingerprints(root)
    for role in ("server", "app", "admin"):
        key = root / role / "tls.key"
        assert key.is_file() and not key.is_symlink()
        custody = key.stat(follow_symlinks=False)
        assert custody.st_uid == os.getuid() and custody.st_mode & 0o777 == 0o400
        if custody.st_gid != os.getgid():
            os.chown(key, -1, os.getgid(), follow_symlinks=False)
        after = key.stat(follow_symlinks=False)
        assert (after.st_uid, after.st_mode) == (custody.st_uid, custody.st_mode)
        assert after.st_gid == os.getgid()
    assert fingerprints(root) == before
    for key in tuple(os.environ):
        if key.startswith("CORTEX_DB_TLS_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CORTEX_DB_AUTH", "certificate")
    for role in ("app", "admin"):
        for name, filename in (("CA", "ca.crt"), ("CERT", "tls.crt"), ("KEY", "tls.key")):
            monkeypatch.setenv(f"CORTEX_DB_TLS_{role.upper()}_{name}", str(root / role / filename))
    return root


def fingerprints(root):
    return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def handshake(client, root, hostname="cortex-pg"):
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(root / "server/tls.crt", root / "server/tls.key")
    server.load_verify_locations(root / "server/ca.crt")
    server.verify_mode = ssl.CERT_REQUIRED
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client_conn = client.wrap_bio(client_in, client_out, server_hostname=hostname)
    server_conn = server.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(10):
        for name, connection in (("client", client_conn), ("server", server_conn)):
            try:
                connection.do_handshake()
                completed.add(name)
            except ssl.SSLWantReadError:
                pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
        if len(completed) == 2:
            return server_conn.getpeercert()
    pytest.fail("TLS handshake did not complete")


def test_fresh_pki_noop_preserves_every_byte_and_mtime(pki):
    before = fingerprints(pki)
    result = initialize(pki)
    assert result.returncode == 0, result.stderr
    assert "no changes" in result.stdout
    assert fingerprints(pki) == before
    assert (pki / "ca/ca.key").stat().st_mode & 0o777 == 0o400
    assert all(not (pki / role / "ca.key").exists() for role in ("server", "app", "admin"))


@pytest.mark.parametrize("role,dsn,cn", [("app", APP_DSN, "cortex_app"), ("admin", ADMIN_DSN, "cortex-admin")])
def test_verified_mutual_tls_uses_separate_client_identities(pki, role, dsn, cn):
    options = db_tls.connection_kwargs(dsn, role)
    context = options["ssl"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    peer = handshake(context, pki)
    assert peer["subject"] == ((("commonName", cn),),)
    with pytest.raises(RuntimeError, match="refuses password"):
        options["password"]()


def test_wrong_server_hostname_is_rejected(pki):
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(db_tls.connection_kwargs(APP_DSN)["ssl"], pki, "wrong-host")


def test_server_rejects_missing_client_certificate(pki):
    context = ssl.create_default_context(cafile=pki / "app/ca.crt")
    with pytest.raises(ssl.SSLError):
        handshake(context, pki)


def test_untrusted_server_is_rejected(pki):
    context = ssl.create_default_context()
    context.load_cert_chain(pki / "app/tls.crt", pki / "app/tls.key")
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(context, pki)


def test_explicit_rotation_replaces_leaves_preserves_ca_and_authenticates(pki):
    before = fingerprints(pki)
    result = initialize(pki, "rotate-leaves")
    assert result.returncode == 0, result.stderr
    after = fingerprints(pki)
    for name in ("ca/ca.crt", "ca/ca.key"):
        assert after[name] == before[name]
    for role in ("server", "app", "admin"):
        for name in ("tls.crt", "tls.key"):
            assert after[f"{role}/{name}"][0] != before[f"{role}/{name}"][0]
    handshake(db_tls.connection_kwargs(APP_DSN)["ssl"], pki)
    assert initialize(pki).returncode == 0


def test_partial_pki_never_silently_generates_new_ca(pki):
    before = (pki / "ca/ca.crt").read_bytes()
    (pki / "app/tls.key").unlink()
    result = initialize(pki)
    assert result.returncode != 0 and "partial PKI" in result.stderr
    assert (pki / "ca/ca.crt").read_bytes() == before
    assert not (pki / "app/tls.key").exists()


def test_rotation_without_ca_refuses(pki):
    (pki / "ca/ca.key").unlink()
    before = fingerprints(pki)
    assert initialize(pki, "rotate-leaves").returncode != 0
    assert fingerprints(pki) == before


def test_mismatched_private_key_fails_before_connect(pki, monkeypatch):
    monkeypatch.setenv("CORTEX_DB_TLS_APP_KEY", str(pki / "admin/tls.key"))
    with pytest.raises(ssl.SSLError):
        db_tls.connection_kwargs(APP_DSN)


def test_missing_private_key_fails_before_connect(pki):
    (pki / "app/tls.key").unlink()
    with pytest.raises(RuntimeError, match="missing"):
        db_tls.connection_kwargs(APP_DSN)


def test_permissive_key_fails_initializer_and_api(pki):
    (pki / "app/tls.key").chmod(0o644)
    with pytest.raises(RuntimeError, match="only to its owner"):
        db_tls.connection_kwargs(APP_DSN)
    assert initialize(pki).returncode != 0


@pytest.mark.parametrize("dsn", [
    "postgresql://cortex_app:secret@cortex-pg/cortex",
    "postgresql://postgres@cortex-pg/cortex",
    "postgresql://cortex_app@/cortex",
    "postgresql://cortex_app@%2Ftmp/cortex",
    APP_DSN + "?sslmode=disable",
    APP_DSN + "?password=secret",
    APP_DSN + "?host=other-host",
    APP_DSN + "?user=postgres",
    APP_DSN + "?passfile=/tmp/creds",
])
def test_certificate_mode_rejects_passwords_role_or_tls_override(pki, dsn):
    with pytest.raises(RuntimeError):
        db_tls.connection_kwargs(dsn)


def test_embedded_mode_preserves_existing_dsn_configuration(monkeypatch):
    monkeypatch.delenv("CORTEX_DB_AUTH", raising=False)
    assert db_tls.connection_kwargs("postgresql://harness:harness@localhost/harness_app", "admin") == {}
    monkeypatch.setenv("CORTEX_DB_AUTH", "embedded")
    assert db_tls.connection_kwargs("postgresql://cortex_app:password@pg/cortex?sslmode=require") == {}


def test_unknown_mode_never_falls_back_to_embedded(monkeypatch):
    monkeypatch.setenv("CORTEX_DB_AUTH", "certficate")
    with pytest.raises(RuntimeError, match="embedded or certificate"):
        db_tls.connection_kwargs(APP_DSN)
