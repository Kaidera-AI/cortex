"""Inactive API certificate custody: real cryptography/TLS, no live services."""

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib
import ipaddress
import os
from pathlib import Path
import ssl
import stat
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import api_tls


INSTANCE = "c7c62a3a-a403-4ed1-b8c8-6b8f960b180c"
OTHER_INSTANCE = "88632553-9754-4a7e-bd94-0daae59aef30"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
DNS = ("localhost", "cortex-api")
IPS = ("127.0.0.1", "::1")


@pytest.fixture
def dirs(tmp_path):
    root = tmp_path.resolve()
    issuer, server = root / "issuer", root / "server"
    issuer.mkdir(mode=0o700)
    server.mkdir(mode=0o700)
    return issuer, server


@pytest.fixture
def custody(dirs):
    api_tls.initialize(*dirs, instance_id=INSTANCE)
    return dirs


def snapshot(dirs):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode))
            for root in dirs for p in root.iterdir() if p.name not in {".issuer.lock", ".server.lock"}}


def private_key(path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def cert(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def public_bytes(key):
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def overwrite(path, content, mode=0o400):
    path.chmod(0o600)
    path.write_bytes(content)
    path.chmod(mode)


def write_leaf(dirs, *, start=NOW - timedelta(minutes=1), end=NOW + timedelta(days=2),
               eku=ExtendedKeyUsageOID.SERVER_AUTH):
    """Create intentionally altered leaves using independent library primitives."""
    issuer, server = dirs
    ca = cert(issuer / "issuer.pem")
    old = cert(server / "server.pem")
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (x509.CertificateBuilder().subject_name(old.subject).issuer_name(ca.subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(start).not_valid_after(end))
    for extension in old.extensions:
        if isinstance(extension.value, x509.ExtendedKeyUsage):
            value = x509.ExtendedKeyUsage([eku])
        elif isinstance(extension.value, x509.SubjectKeyIdentifier):
            value = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
        else:
            value = extension.value
        builder = builder.add_extension(value, critical=extension.critical)
    leaf = builder.sign(private_key(issuer / "issuer.pem"), hashes.SHA256())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()) + leaf.public_bytes(serialization.Encoding.PEM)
    overwrite(server / "server.pem", pem)


def handshake(server_dir, trust_file, hostname):
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.minimum_version = ssl.TLSVersion.TLSv1_2
    server.load_cert_chain(server_dir / "server.pem")
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.minimum_version = ssl.TLSVersion.TLSv1_2
    client.load_verify_locations(cafile=trust_file)
    assert client.check_hostname and client.verify_mode == ssl.CERT_REQUIRED
    incoming_client, outgoing_client, incoming_server, outgoing_server = (
        ssl.MemoryBIO() for _ in range(4))
    peer = client.wrap_bio(incoming_client, outgoing_client, server_hostname=hostname)
    origin = server.wrap_bio(incoming_server, outgoing_server, server_side=True)
    done = set()
    for _ in range(32):
        for name, connection in (("client", peer), ("server", origin)):
            try:
                connection.do_handshake()
                done.add(name)
            except ssl.SSLWantReadError:
                pass
        incoming_server.write(outgoing_client.read())
        incoming_client.write(outgoing_server.read())
        if len(done) == 2:
            break
    assert done == {"client", "server"}, "bounded TLS handshake did not complete"
    assert peer.version() in {"TLSv1.2", "TLSv1.3"}
    assert origin.getpeercert() is None, "API TLS must not require client mTLS"
    origin.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    incoming_client.write(outgoing_server.read())
    assert peer.read(4096).endswith(b"\r\n\r\nok")


def test_fresh_profile_inventory_and_separate_keys(dirs):
    result = api_tls.initialize(*dirs, instance_id=INSTANCE)
    issuer, server = dirs
    assert result["status"] == "created"
    assert set(p.name for p in issuer.iterdir()) == {"issuer.pem", ".issuer.lock"}
    assert set(p.name for p in server.iterdir()) == {"server.pem", "ca.crt", ".server.lock"}
    assert stat.S_IMODE(issuer.stat().st_mode) == stat.S_IMODE(server.stat().st_mode) == 0o700
    for path in (issuer / "issuer.pem", server / "server.pem"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert path.stat().st_uid == os.geteuid() and path.stat().st_nlink == 1
    assert stat.S_IMODE((server / "ca.crt").stat().st_mode) == 0o444
    ca, leaf = cert(issuer / "issuer.pem"), cert(server / "server.pem")
    assert ca.public_bytes(serialization.Encoding.PEM) == (server / "ca.crt").read_bytes()
    assert b"PRIVATE KEY" not in (server / "ca.crt").read_bytes()
    assert public_bytes(private_key(issuer / "issuer.pem")) != public_bytes(private_key(server / "server.pem"))
    assert isinstance(private_key(issuer / "issuer.pem").curve, ec.SECP256R1)
    assert isinstance(private_key(server / "server.pem").curve, ec.SECP256R1)
    ca.verify_directly_issued_by(ca)
    leaf.verify_directly_issued_by(ca)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value == x509.BasicConstraints(True, 0)
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value == x509.BasicConstraints(False, None)
    assert list(leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert ca.not_valid_after_utc - ca.not_valid_before_utc == timedelta(days=1825)
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc == timedelta(days=90)
    assert leaf.not_valid_after_utc <= ca.not_valid_after_utc
    for certificate in (ca, leaf):
        assert INSTANCE in certificate.subject.rfc4514_string()
        assert certificate.signature_hash_algorithm.name == "sha256"
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(sans.get_values_for_type(x509.DNSName)) == set(DNS)
    assert set(sans.get_values_for_type(x509.IPAddress)) == {ipaddress.ip_address(v) for v in IPS}
    assert result["ca_sha256"] == hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest()


@pytest.mark.parametrize("hostname", ["localhost", "cortex-api", "127.0.0.1", "::1"])
def test_actual_verified_handshake_and_application_bytes(custody, hostname):
    handshake(custody[1], custody[1] / "ca.crt", hostname)


@pytest.mark.parametrize("hostname", ["other.local", "127.0.0.2", "::2"])
def test_wrong_dns_or_ip_rejected_by_real_tls(custody, hostname):
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(custody[1], custody[1] / "ca.crt", hostname)


def test_other_instance_ca_cannot_validate_real_tls(custody, tmp_path):
    other_issuer, other_server = tmp_path / "other-issuer", tmp_path / "other-server"
    other_issuer.mkdir(mode=0o700)
    other_server.mkdir(mode=0o700)
    api_tls.initialize(other_issuer, other_server, instance_id=OTHER_INSTANCE)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(custody[1], other_server / "ca.crt", "localhost")


@pytest.mark.parametrize("kind", ["expired", "future", "client-eku"])
def test_real_tls_rejects_invalid_leaf_lifetime_or_purpose(custody, kind):
    options = {"expired": {"start": NOW - timedelta(days=5), "end": NOW - timedelta(days=1)},
               "future": {"start": NOW + timedelta(days=1), "end": NOW + timedelta(days=2)},
               "client-eku": {"eku": ExtendedKeyUsageOID.CLIENT_AUTH}}[kind]
    write_leaf(custody, **options)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(custody[1], custody[1] / "ca.crt", "localhost")
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.validate_server(custody[1], instance_id=INSTANCE)


def test_validate_server_requires_no_issuer_private_directory(custody):
    issuer, server = custody
    issuer.rename(issuer.with_name("offline-issuer"))
    result = api_tls.validate_server(server, instance_id=INSTANCE)
    assert result["status"] == "valid"
    assert "PRIVATE KEY" not in repr(result)
    assert all("-----BEGIN" not in str(v) for v in result.values())


def test_repeated_initialize_preserves_artifact_bytes_modes_and_mtime(custody):
    before = snapshot(custody)
    assert api_tls.initialize(*custody, instance_id=INSTANCE)["status"] == "unchanged"
    assert snapshot(custody) == before


def test_explicit_additional_names_normalized_and_verified(dirs):
    options = {"dns_names": (*DNS, "My-Cortex.EXAMPLE"), "ip_addresses": (*IPS, "192.0.2.8")}
    api_tls.initialize(*dirs, instance_id=INSTANCE, **options)
    api_tls.validate_server(dirs[1], instance_id=INSTANCE, **options)
    handshake(dirs[1], dirs[1] / "ca.crt", "my-cortex.example")
    handshake(dirs[1], dirs[1] / "ca.crt", "192.0.2.8")
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.validate_server(dirs[1], instance_id=INSTANCE)


@pytest.mark.parametrize("dns_names", [(), ("",), ("*",), ("*.example",), ("https://example",),
    ("example:8501",), (" example",), ("example.",), ("a..b",), ("a_b",), ("-a",),
    ("a-",), ("é.example",), ("a" * 64,), ("localhost", "LOCALHOST"), ("127.0.0.1",)])
def test_bad_dns_names_refuse_before_material_creation(dirs, dns_names):
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*dirs, instance_id=INSTANCE, dns_names=dns_names)
    assert not (dirs[0] / "issuer.pem").exists()
    assert not (dirs[1] / "server.pem").exists()


@pytest.mark.parametrize("ips", [("999.0.0.1",), (" localhost",), ("localhost",), ("127.0.0.1:80",),
                                 ("fe80::1%en0",), ("::1", "0:0:0:0:0:0:0:1")])
def test_bad_ip_inputs_refuse(dirs, ips):
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*dirs, instance_id=INSTANCE, ip_addresses=ips)


@pytest.mark.parametrize("instance_id", ["", "cortex", "../cortex", " " + INSTANCE, None])
def test_invalid_instance_identifier_refuses(dirs, instance_id):
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*dirs, instance_id=instance_id)


@pytest.mark.parametrize("operation", ["initialize", "renew_leaf"])
@pytest.mark.parametrize("change", ["instance", "sans"])
def test_foreign_identity_or_changed_sans_never_adopted(custody, operation, change):
    before = snapshot(custody)
    options = {"instance_id": OTHER_INSTANCE} if change == "instance" else {
        "instance_id": INSTANCE, "dns_names": (*DNS, "new.example")}
    with pytest.raises(api_tls.TLSCustodyError):
        getattr(api_tls, operation)(*custody, **options)
    assert snapshot(custody) == before


@pytest.mark.parametrize("kind", ["issuer-only", "issuer-and-public"])
def test_supported_partial_init_resumes_same_issuer(custody, kind):
    issuer, server = custody
    (server / "server.pem").unlink()
    if kind == "issuer-only":
        (server / "ca.crt").unlink()
    before = snapshot(custody)
    assert api_tls.initialize(*custody, instance_id=INSTANCE)["status"] == "resumed"
    after = snapshot(custody)
    assert all(after[path] == value for path, value in before.items())
    handshake(server, server / "ca.crt", "localhost")


@pytest.mark.parametrize("absent", [("issuer",), ("public",), ("issuer", "leaf"), ("issuer", "public")])
def test_unsupported_partial_init_refuses_without_changing_remaining_files(custody, absent):
    files = {"issuer": custody[0] / "issuer.pem", "public": custody[1] / "ca.crt", "leaf": custody[1] / "server.pem"}
    for name in absent:
        files[name].unlink()
    before = snapshot(custody)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)
    assert snapshot(custody) == before


@pytest.mark.parametrize("which", ["issuer", "leaf", "public"])
@pytest.mark.parametrize("damage", ["garbage", "extra-certificate", "extra-key", "oversized"])
def test_malformed_or_extra_material_is_never_adopted(custody, which, damage):
    path = {"issuer": custody[0] / "issuer.pem", "leaf": custody[1] / "server.pem", "public": custody[1] / "ca.crt"}[which]
    content = path.read_bytes()
    additions = {"garbage": b"not-a-certificate", "extra-certificate": cert(custody[0] / "issuer.pem").public_bytes(serialization.Encoding.PEM),
                 "extra-key": private_key(custody[0] / "issuer.pem").private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
                 "oversized": b"x" * (1024 * 1024 + 1)}
    overwrite(path, content + additions[damage], 0o444 if which == "public" else 0o400)
    before = snapshot(custody)
    with pytest.raises(api_tls.TLSCustodyError) as error:
        api_tls.initialize(*custody, instance_id=INSTANCE)
    assert "PRIVATE KEY" not in str(error.value)
    assert snapshot(custody) == before


def test_mismatched_leaf_private_key_refuses_without_secret_error(custody):
    issuer, server = custody
    key_pem = private_key(issuer / "issuer.pem").private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    overwrite(server / "server.pem", key_pem + cert(server / "server.pem").public_bytes(serialization.Encoding.PEM))
    with pytest.raises(api_tls.TLSCustodyError) as error:
        api_tls.validate_server(server, instance_id=INSTANCE)
    assert key_pem.decode().splitlines()[1] not in str(error.value)


@pytest.mark.parametrize("target", ["issuer", "server", "leaf", "public", "lock", "server-lock"])
@pytest.mark.parametrize("unsafe", ["symlink", "mode"])
def test_custody_path_symlinks_and_unsafe_modes_refuse(custody, target, unsafe):
    issuer, server = custody
    path = {"issuer": issuer, "server": server, "leaf": server / "server.pem",
            "public": server / "ca.crt", "lock": issuer / ".issuer.lock",
            "server-lock": server / ".server.lock"}[target]
    if unsafe == "symlink":
        moved = path.with_name(path.name + ".saved")
        path.rename(moved)
        path.symlink_to(moved)
    else:
        path.chmod(0o777 if path.is_dir() else 0o666)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)


@pytest.mark.parametrize("target", ["issuer.pem", "server.pem", "ca.crt", ".issuer.lock", ".server.lock"])
@pytest.mark.parametrize("unsafe", ["hardlink", "fifo"])
def test_hardlinks_and_special_files_fail_without_blocking(custody, target, unsafe, tmp_path):
    path = (custody[0] if target in {"issuer.pem", ".issuer.lock"} else custody[1]) / target
    if unsafe == "hardlink":
        os.link(path, tmp_path / (target + ".linked"))
    else:
        path.unlink()
        os.mkfifo(path, 0o400)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)


def test_wrong_owner_refuses_without_privileged_chown(custody, monkeypatch):
    actual = os.geteuid()
    monkeypatch.setattr(api_tls.os, "geteuid", lambda: actual + 1)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)


@pytest.mark.parametrize("kind", ["same", "issuer-inside-server", "server-inside-issuer"])
def test_issuer_and_server_roots_cannot_overlap(dirs, kind):
    issuer, server = dirs
    if kind == "same":
        server = issuer
    elif kind == "issuer-inside-server":
        issuer = server / "issuer"
        issuer.mkdir(mode=0o700)
    else:
        server = issuer / "server"
        server.mkdir(mode=0o700)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(issuer, server, instance_id=INSTANCE)


def test_locked_writer_fails_bounded_without_modifying_artifacts(custody):
    before = snapshot(custody)
    with (custody[0] / ".issuer.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(api_tls.TLSCustodyError):
            api_tls.renew_leaf(*custody, instance_id=INSTANCE)
    assert snapshot(custody) == before
    assert api_tls.initialize(*custody, instance_id=INSTANCE)["status"] == "unchanged"


def test_shared_server_lock_blocks_a_different_issuer_selection(custody, tmp_path):
    other_issuer = tmp_path / "other-issuer"
    other_issuer.mkdir(mode=0o700)
    before = snapshot(custody)
    with (custody[1] / ".server.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(api_tls.TLSCustodyError):
            api_tls.initialize(other_issuer, custody[1], instance_id=INSTANCE)
    assert not (other_issuer / "issuer.pem").exists()
    assert snapshot(custody) == before


def test_alias_custody_roots_refuse(dirs):
    issuer, server = dirs
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(issuer, server / ".." / "issuer", instance_id=INSTANCE)
    assert not (issuer / "issuer.pem").exists()


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_init_publication_fault_resumes_only_same_issuer(dirs, monkeypatch, fail_at):
    real_link = api_tls.os.link
    calls = 0
    def broken_link(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("synthetic-publish-failure")
        return real_link(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(api_tls.os, "link", broken_link)
        with pytest.raises(api_tls.TLSCustodyError):
            api_tls.initialize(*dirs, instance_id=INSTANCE)
    before = snapshot(dirs)
    assert len(before) == fail_at - 1
    api_tls.initialize(*dirs, instance_id=INSTANCE)
    after = snapshot(dirs)
    assert all(after[path] == value for path, value in before.items())
    handshake(dirs[1], dirs[1] / "ca.crt", "localhost")


@pytest.mark.parametrize("target", ["issuer.pem", "ca.crt", "server.pem"])
def test_absent_file_race_refuses_without_overwriting_unknown_target(dirs, monkeypatch, target):
    real_link = api_tls.os.link
    marker = b"unknown-concurrent-writer"
    def racing_link(src, dst, **kwargs):
        if Path(dst).name == target:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400,
                         dir_fd=kwargs.get("dst_dir_fd"))
            try:
                os.write(fd, marker)
            finally:
                os.close(fd)
        return real_link(src, dst, **kwargs)
    monkeypatch.setattr(api_tls.os, "link", racing_link)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*dirs, instance_id=INSTANCE)
    path = (dirs[0] if target == "issuer.pem" else dirs[1]) / target
    assert path.read_bytes() == marker


def test_renewal_preserves_ca_and_changes_leaf_key_and_serial(custody):
    before = snapshot(custody)
    old_key = public_bytes(private_key(custody[1] / "server.pem"))
    old_serial = cert(custody[1] / "server.pem").serial_number
    assert api_tls.renew_leaf(*custody, instance_id=INSTANCE)["status"] == "renewed"
    after = snapshot(custody)
    for path in (custody[0] / "issuer.pem", custody[1] / "ca.crt"):
        assert before[str(path)] == after[str(path)]
    assert old_key != public_bytes(private_key(custody[1] / "server.pem"))
    assert old_serial != cert(custody[1] / "server.pem").serial_number
    handshake(custody[1], custody[1] / "ca.crt", "localhost")


def test_expired_leaf_needs_explicit_renewal_not_init(custody):
    write_leaf(custody, start=NOW - timedelta(days=95), end=NOW - timedelta(days=5))
    before = snapshot(custody)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)
    assert snapshot(custody) == before
    api_tls.renew_leaf(*custody, instance_id=INSTANCE)
    handshake(custody[1], custody[1] / "ca.crt", "localhost")


@pytest.mark.parametrize("days_elapsed", [1736, 1826])
def test_insufficient_or_expired_ca_refuses_renewal(custody, monkeypatch, days_elapsed):
    before = snapshot(custody)
    base = cert(custody[0] / "issuer.pem").not_valid_before_utc
    monkeypatch.setattr(api_tls, "_now", lambda: base + timedelta(days=days_elapsed))
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.renew_leaf(*custody, instance_id=INSTANCE)
    assert snapshot(custody) == before


@pytest.mark.parametrize("fault", ["write", "replace", "sign"])
def test_renewal_precommit_failure_preserves_old_leaf_and_releases_lock(custody, monkeypatch, fault):
    before = snapshot(custody)
    def fail(*args, **kwargs):
        raise OSError("synthetic-renewal-failure")
    with monkeypatch.context() as patch:
        if fault == "sign":
            patch.setattr(x509.CertificateBuilder, "sign", fail)
        else:
            patch.setattr(api_tls.os, "write" if fault == "write" else "replace", fail)
        with pytest.raises(api_tls.TLSCustodyError):
            api_tls.renew_leaf(*custody, instance_id=INSTANCE)
    assert snapshot(custody) == before
    handshake(custody[1], custody[1] / "ca.crt", "localhost")
    assert api_tls.initialize(*custody, instance_id=INSTANCE)["status"] == "unchanged"


def test_postrename_directory_fsync_failure_is_not_reported_as_rollback(custody, monkeypatch):
    before = snapshot(custody)
    real_replace, real_fsync = api_tls.os.replace, api_tls.os.fsync
    committed = False
    def replaced(*args, **kwargs):
        nonlocal committed
        result = real_replace(*args, **kwargs)
        committed = True
        return result
    def broken_fsync(fd):
        if committed and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic-postcommit-fsync")
        return real_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(api_tls.os, "replace", replaced)
        patch.setattr(api_tls.os, "fsync", broken_fsync)
        with pytest.raises(api_tls.TLSCustodyError, match="uncertain durability"):
            api_tls.renew_leaf(*custody, instance_id=INSTANCE)
    assert committed
    after = snapshot(custody)
    assert after[str(custody[1] / "server.pem")][0] != before[str(custody[1] / "server.pem")][0]
    assert after[str(custody[0] / "issuer.pem")] == before[str(custody[0] / "issuer.pem")]
    api_tls.validate_server(custody[1], instance_id=INSTANCE)
    handshake(custody[1], custody[1] / "ca.crt", "localhost")


@pytest.mark.parametrize("target", ["leaf", "server-root", "issuer-root"])
def test_renewal_precommit_rechecks_leaf_and_directory_identity(custody, monkeypatch, target):
    issuer, server = custody
    before_issuer = (issuer / "issuer.pem").read_bytes()
    real_fsync = api_tls.os.fsync
    changed = False
    foreign = b"foreign-precommit-replacement"
    def swap_after_stage(fd):
        nonlocal changed
        result = real_fsync(fd)
        if not changed and stat.S_ISREG(os.fstat(fd).st_mode):
            changed = True
            if target == "leaf":
                (server / "server.pem").unlink()
                (server / "server.pem").write_bytes(foreign)
                (server / "server.pem").chmod(0o400)
            else:
                selected = server if target == "server-root" else issuer
                selected.rename(selected.with_name(selected.name + ".previous"))
                selected.mkdir(mode=0o700)
                (selected / "foreign").write_bytes(foreign)
        return result
    monkeypatch.setattr(api_tls.os, "fsync", swap_after_stage)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.renew_leaf(*custody, instance_id=INSTANCE)
    assert changed
    if target == "leaf":
        assert (server / "server.pem").read_bytes() == foreign
        assert (issuer / "issuer.pem").read_bytes() == before_issuer
    else:
        selected = server if target == "server-root" else issuer
        assert set(p.name for p in selected.iterdir()) == {"foreign"}
        assert (selected / "foreign").read_bytes() == foreign


def test_failure_cleanup_preserves_foreign_replacement_of_staging_name(dirs, monkeypatch):
    real_link = api_tls.os.link
    saved = None
    foreign = b"foreign-stage-entry"
    def replace_stage_then_fail(src, dst, **kwargs):
        nonlocal saved
        directory = kwargs.get("src_dir_fd")
        os.unlink(src, dir_fd=directory)
        fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=directory)
        try:
            os.write(fd, foreign)
        finally:
            os.close(fd)
        saved = Path(src) if Path(src).is_absolute() else dirs[0] / src
        raise OSError("synthetic-replaced-stage")
    monkeypatch.setattr(api_tls.os, "link", replace_stage_then_fail)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*dirs, instance_id=INSTANCE)
    assert saved is not None and saved.read_bytes() == foreign


def test_unexplained_staging_file_is_not_deleted_during_renewal(custody):
    unexplained = custody[1] / ".api-tls-unexplained"
    unexplained.write_bytes(b"operator-owned-unrelated-file")
    before = unexplained.read_bytes()
    api_tls.renew_leaf(*custody, instance_id=INSTANCE)
    assert unexplained.read_bytes() == before


def test_postgresql_shaped_self_signed_root_is_not_an_api_issuer(custody):
    issuer, server = custody
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Cortex PostgreSQL CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(NOW - timedelta(minutes=1))
          .not_valid_after(NOW + timedelta(days=1825))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(key, hashes.SHA256()))
    overwrite(issuer / "issuer.pem", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
              serialization.NoEncryption()) + ca.public_bytes(serialization.Encoding.PEM))
    overwrite(server / "ca.crt", ca.public_bytes(serialization.Encoding.PEM), 0o444)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)


@pytest.mark.parametrize("role,defect", [
    (role, defect) for role in ("issuer", "leaf")
    for defect in ("basic-criticality", "ca-constraint", "key-usage", "ski", "aki", "instance", "hash")
] + [("issuer", "path-length"), ("leaf", "eku"), ("leaf", "ski-criticality")])
def test_cryptographically_valid_wrong_profile_is_rejected(custody, role, defect):
    issuer, server = custody
    ca_key, leaf_key = private_key(issuer / "issuer.pem"), private_key(server / "server.pem")
    ca, leaf = cert(issuer / "issuer.pem"), cert(server / "server.pem")

    def rebuild(old, key, issuing_key, issuing_name, current_role):
        subject = old.subject
        if role == current_role and defect == "instance":
            subject = x509.Name([x509.NameAttribute(attribute.oid, attribute.value.replace(INSTANCE, OTHER_INSTANCE))
                                 for attribute in old.subject])
        if current_role == "issuer":
            issuing_name = subject
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuing_name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(old.not_valid_before_utc).not_valid_after(old.not_valid_after_utc))
        for extension in old.extensions:
            value, critical = extension.value, extension.critical
            if role == current_role:
                if isinstance(value, x509.BasicConstraints):
                    if defect == "basic-criticality":
                        critical = not critical
                    elif defect == "ca-constraint":
                        value = x509.BasicConstraints(not value.ca, 0 if not value.ca else None)
                    elif defect == "path-length":
                        value = x509.BasicConstraints(True, 1)
                elif isinstance(value, x509.KeyUsage) and defect == "key-usage":
                    value = x509.KeyUsage(True, False, False, False, False, role != "issuer", False, False, False)
                elif isinstance(value, x509.SubjectKeyIdentifier):
                    if defect == "ski":
                        value = x509.SubjectKeyIdentifier(b"a" * 20)
                    elif defect == "ski-criticality":
                        critical = not critical
                elif isinstance(value, x509.AuthorityKeyIdentifier) and defect == "aki":
                    value = x509.AuthorityKeyIdentifier(b"b" * 20, None, None)
                elif isinstance(value, x509.ExtendedKeyUsage) and defect == "eku":
                    value = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH])
            builder = builder.add_extension(value, critical=critical)
        algorithm = hashes.SHA384() if role == current_role and defect == "hash" else hashes.SHA256()
        return builder.sign(issuing_key, algorithm)

    new_ca = rebuild(ca, ca_key, ca_key, ca.subject, "issuer")
    new_leaf = rebuild(leaf, leaf_key, ca_key, new_ca.subject, "leaf")
    new_ca.verify_directly_issued_by(new_ca)
    new_leaf.verify_directly_issued_by(new_ca)
    for path, key, certificate in ((issuer / "issuer.pem", ca_key, new_ca), (server / "server.pem", leaf_key, new_leaf)):
        overwrite(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                  serialization.NoEncryption()) + certificate.public_bytes(serialization.Encoding.PEM))
    overwrite(server / "ca.crt", new_ca.public_bytes(serialization.Encoding.PEM), 0o444)
    before = snapshot(custody)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.validate_server(server, instance_id=INSTANCE)
    assert snapshot(custody) == before


def test_import_has_no_custody_environment_or_network_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("import attempted custody/configuration work")
    with monkeypatch.context() as patch:
        patch.setattr(api_tls.os, "getenv", forbidden)
        patch.setattr(api_tls.os, "open", forbidden)
        patch.setattr(api_tls.Path, "read_bytes", forbidden)
        importlib.reload(api_tls)


def test_validly_signed_leaf_must_not_expose_the_issuer_private_key(custody):
    issuer, server = custody
    ca_key, ca, leaf = private_key(issuer / "issuer.pem"), cert(issuer / "issuer.pem"), cert(server / "server.pem")
    builder = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(ca.subject)
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(leaf.not_valid_before_utc).not_valid_after(leaf.not_valid_after_utc))
    for extension in leaf.extensions:
        value = (x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
                 if isinstance(extension.value, x509.SubjectKeyIdentifier) else extension.value)
        builder = builder.add_extension(value, critical=extension.critical)
    unsafe_leaf = builder.sign(ca_key, hashes.SHA256())
    unsafe_leaf.verify_directly_issued_by(ca)
    overwrite(server / "server.pem", ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
              serialization.NoEncryption()) + unsafe_leaf.public_bytes(serialization.Encoding.PEM))
    handshake(server, server / "ca.crt", "localhost")
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.validate_server(server, instance_id=INSTANCE)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.renew_leaf(*custody, instance_id=INSTANCE)


@pytest.mark.parametrize("role", ["issuer", "leaf"])
def test_missing_required_extension_replaced_with_unknown_extension_refuses_generically(custody, role):
    issuer, server = custody
    ca_key, ca = private_key(issuer / "issuer.pem"), cert(issuer / "issuer.pem")
    path = issuer / "issuer.pem" if role == "issuer" else server / "server.pem"
    original, key = cert(path), private_key(path)
    builder = (x509.CertificateBuilder().subject_name(original.subject).issuer_name(original.issuer)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(original.not_valid_before_utc).not_valid_after(original.not_valid_after_utc))
    for extension in original.extensions:
        value = (x509.UnrecognizedExtension(x509.ObjectIdentifier("1.2.3.4.5"), b"\x05\x00")
                 if isinstance(extension.value, x509.SubjectKeyIdentifier) else extension.value)
        builder = builder.add_extension(value, critical=extension.critical)
    changed = builder.sign(ca_key, hashes.SHA256())
    changed.verify_directly_issued_by(changed if role == "issuer" else ca)
    overwrite(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
              serialization.NoEncryption()) + changed.public_bytes(serialization.Encoding.PEM))
    if role == "issuer":
        overwrite(server / "ca.crt", changed.public_bytes(serialization.Encoding.PEM), 0o444)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.validate_server(server, instance_id=INSTANCE)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)


def test_parent_traversal_cannot_erase_a_symlink_from_selected_path(dirs, tmp_path):
    alias = tmp_path / "aliased-directory"
    destination = tmp_path / "different" / "child"
    destination.mkdir(parents=True)
    alias.symlink_to(destination)
    lexical_issuer = alias / ".." / "issuer"
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(lexical_issuer, dirs[1], instance_id=INSTANCE)
    assert not (dirs[0] / "issuer.pem").exists()
    assert not (dirs[1] / "server.pem").exists()


@pytest.mark.parametrize("target", ["issuer", "server"])
def test_noop_rechecks_selected_directory_identity_after_reading(custody, monkeypatch, target):
    real_check = api_tls._check_certificate
    selected = custody[0] if target == "issuer" else custody[1]
    changed = False
    def change_after_leaf_check(*args, **kwargs):
        nonlocal changed
        result = real_check(*args, **kwargs)
        if not kwargs.get("issuer", False) and not changed:
            changed = True
            selected.rename(selected.with_name(selected.name + ".previous"))
            selected.mkdir(mode=0o700)
            (selected / "foreign").write_bytes(b"new-directory-identity")
        return result
    monkeypatch.setattr(api_tls, "_check_certificate", change_after_leaf_check)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*custody, instance_id=INSTANCE)
    assert changed
    assert set(p.name for p in selected.iterdir()) == {"foreign"}


@pytest.mark.parametrize("target", ["issuer", "server"])
@pytest.mark.parametrize("boundary", ["staging", "between-publications", "after-final-publication"])
def test_initial_publication_never_reports_success_for_replaced_selected_roots(dirs, monkeypatch, target, boundary):
    selected = dirs[0] if target == "issuer" else dirs[1]
    real_fsync, real_link = api_tls.os.fsync, api_tls.os.link
    changed = False
    links = 0
    def swap():
        nonlocal changed
        changed = True
        selected.rename(selected.with_name(selected.name + ".previous"))
        selected.mkdir(mode=0o700)
        (selected / "foreign").write_bytes(b"new-selected-root")
    def fsync_after_stage(fd):
        result = real_fsync(fd)
        if boundary == "staging" and not changed and stat.S_ISREG(os.fstat(fd).st_mode):
            swap()
        return result
    def link_then_swap(*args, **kwargs):
        nonlocal links
        result = real_link(*args, **kwargs)
        links += 1
        if not changed and ((boundary == "between-publications" and links == 1) or
                            (boundary == "after-final-publication" and links == 3)):
            swap()
        return result
    monkeypatch.setattr(api_tls.os, "fsync", fsync_after_stage)
    monkeypatch.setattr(api_tls.os, "link", link_then_swap)
    with pytest.raises(api_tls.TLSCustodyError):
        api_tls.initialize(*dirs, instance_id=INSTANCE)
    assert changed
    assert set(p.name for p in selected.iterdir()) == {"foreign"}
    assert (selected / "foreign").read_bytes() == b"new-selected-root"


@pytest.mark.parametrize("operation", ["validate_server", "initialize", "renew_leaf"])
def test_actual_der_unsupported_version_refuses_with_generic_custody_error(custody, operation):
    import base64
    issuer, server = custody
    leaf = cert(server / "server.pem")
    der = leaf.public_bytes(serialization.Encoding.DER)
    version = b"\xa0\x03\x02\x01\x02"
    offset = der.find(version)
    assert 0 <= offset < 12, "expected explicit X.509 v3 version field"
    bad_der = der[:offset + 4] + b"\x03" + der[offset + 5:]
    with pytest.raises(x509.InvalidVersion):
        x509.load_der_x509_certificate(bad_der)
    pem = b"-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(bad_der) + b"-----END CERTIFICATE-----\n"
    key = private_key(server / "server.pem").private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    overwrite(server / "server.pem", key + pem)
    before = snapshot(custody)
    arguments = (server,) if operation == "validate_server" else (issuer, server)
    with pytest.raises(api_tls.TLSCustodyError):
        getattr(api_tls, operation)(*arguments, instance_id=INSTANCE)
    assert snapshot(custody) == before


@pytest.mark.parametrize("operation,target", [
    ("validate_server", "leaf"), ("validate_server", "public"),
    ("initialize", "leaf"), ("initialize", "public"), ("initialize", "issuer")])
def test_readonly_validation_and_noop_recheck_validated_file_records(custody, monkeypatch, operation, target):
    issuer, server = custody
    selected = {"leaf": server / "server.pem", "public": server / "ca.crt", "issuer": issuer / "issuer.pem"}[target]
    real_check = api_tls._check_certificate
    changed = False
    foreign = b"foreign-record-after-certificate-check"
    def replace_record_after_leaf_check(*args, **kwargs):
        nonlocal changed
        result = real_check(*args, **kwargs)
        if not kwargs.get("issuer", False) and not changed:
            changed = True
            selected.unlink()
            selected.write_bytes(foreign)
            selected.chmod(0o444 if target == "public" else 0o400)
        return result
    monkeypatch.setattr(api_tls, "_check_certificate", replace_record_after_leaf_check)
    arguments = (server,) if operation == "validate_server" else (issuer, server)
    with pytest.raises(api_tls.TLSCustodyError):
        getattr(api_tls, operation)(*arguments, instance_id=INSTANCE)
    assert changed
    assert selected.read_bytes() == foreign


@pytest.mark.parametrize("operation", ["create", "resume", "renew"])
@pytest.mark.parametrize("target", ["issuer", "public", "leaf"])
def test_successful_write_receipts_recheck_all_expected_records_after_final_fsync(dirs, monkeypatch, operation, target):
    issuer, server = dirs
    if operation != "create":
        api_tls.initialize(*dirs, instance_id=INSTANCE)
        if operation == "resume":
            (server / "server.pem").unlink()
    selected = {"issuer": issuer / "issuer.pem", "public": server / "ca.crt", "leaf": server / "server.pem"}[target]
    real_fsync = api_tls.os.fsync
    changed = False
    foreign = b"foreign-record-after-final-publication-fsync"
    def replace_after_final_fsync(fd):
        nonlocal changed
        result = real_fsync(fd)
        if not changed and stat.S_ISDIR(os.fstat(fd).st_mode) and (server / "server.pem").exists():
            changed = True
            selected.unlink()
            selected.write_bytes(foreign)
            selected.chmod(0o444 if target == "public" else 0o400)
        return result
    monkeypatch.setattr(api_tls.os, "fsync", replace_after_final_fsync)
    action = api_tls.renew_leaf if operation == "renew" else api_tls.initialize
    with pytest.raises(api_tls.TLSCustodyError, match="uncertain durability" if operation == "renew" else None):
        action(*dirs, instance_id=INSTANCE)
    assert changed
    assert selected.read_bytes() == foreign
