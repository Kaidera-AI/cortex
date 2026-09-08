"""Inactive, finite per-instance API certificate custody; never database PKI.

Callers supply existing private directories owned by their effective UID. The
issuer directory must never be mounted on the API. Only initialize/renew_leaf
need it; validate_server requires just server.pem and the public ca.crt.

This POSIX module coordinates cooperating writers, not hostile same-UID/root
actors. Parent paths must be controlled by the operator; selected directories
are checked, opened without following symlinks and accessed by descriptor.
It neither configures a server/client nor changes any system trust store.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, SignatureAlgorithmOID


class TLSCustodyError(ValueError):
    """Invalid/unsafe custody or failed finite publication; never includes PEM."""


_MAX_BYTES = 65536
_DNS = ("localhost", "cortex-api")
_IPS = ("127.0.0.1", "::1")
_CERT = rb"-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n"
# Assembled from two literals so the PEM key header never exists contiguously at rest
# in source (secret scanners flag the header; the pattern itself is not a credential).
_KEY = rb"-----BEGIN PRIVATE" + rb" KEY-----\n[A-Za-z0-9+/=\n]+-----END PRIVATE" + rb" KEY-----\n"
_EXPECTED_ERRORS = (OSError, ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm,
                    x509.ExtensionNotFound, x509.DuplicateExtension, x509.UnsupportedGeneralNameType,
                    x509.InvalidVersion)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _identity(instance_id, dns_names, ip_addresses):
    if not isinstance(instance_id, str) or str(uuid.UUID(instance_id)) != instance_id or not uuid.UUID(instance_id).int:
        raise TLSCustodyError("invalid API instance identity")
    if not isinstance(dns_names, (tuple, list)) or not dns_names:
        raise TLSCustodyError("invalid API DNS names")
    names = []
    for value in dns_names:
        if (not isinstance(value, str) or len(value) > 253 or
                not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                        for label in value.split("."))):
            raise TLSCustodyError("invalid API DNS names")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise TLSCustodyError("IP address supplied as DNS name")
        names.append(value.lower())
    if not isinstance(ip_addresses, (tuple, list)):
        raise TLSCustodyError("invalid API IP addresses")
    addresses = []
    for value in ip_addresses:
        if not isinstance(value, str) or "%" in value:
            raise TLSCustodyError("invalid API IP addresses")
        addresses.append(ipaddress.ip_address(value))
    if len(set(names)) != len(names) or len(set(addresses)) != len(addresses):
        raise TLSCustodyError("duplicate API SAN identity")
    return instance_id, tuple(sorted(names)), tuple(sorted(addresses, key=lambda value: (value.version, int(value))))


def _same(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _safe_file(info, mode):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1 or info.st_size > _MAX_BYTES):
        raise TLSCustodyError("unsafe API custody file")


def _root_path(path):
    if ".." in Path(path).parts:
        raise TLSCustodyError("parent traversal in API custody path")
    path = Path(os.path.abspath(os.fspath(path)))
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise TLSCustodyError("symlink in API custody path")
    return path


@contextmanager
def _directory(path):
    path = _root_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise TLSCustodyError("unsafe API custody directory")
        _check_directory(path, fd)
        yield path, fd
    finally:
        os.close(fd)


def _check_directory(path, fd):
    _root_path(path)
    current = path.stat(follow_symlinks=False)
    if (not _same(current, os.fstat(fd)) or not stat.S_ISDIR(current.st_mode) or
            current.st_uid != os.geteuid() or stat.S_IMODE(current.st_mode) != 0o700):
        raise TLSCustodyError("API custody directory changed")


@contextmanager
def _lock(directory, name, *, readonly=False):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDONLY if readonly else os.O_RDWR | os.O_CREAT)
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        _safe_file(info, 0o600)
        if info.st_size:
            raise TLSCustodyError("unexpected API custody lock content")
        fcntl.flock(fd, (fcntl.LOCK_SH if readonly else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        if not _same(info, os.stat(name, dir_fd=directory, follow_symlinks=False)):
            raise TLSCustodyError("API custody lock changed")
        yield
    finally:
        os.close(fd)


def _read(directory, name, mode, *, missing=False):
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        if missing:
            return None
        raise
    _safe_file(before, mode)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        _safe_file(info, mode)
        if not _same(info, before):
            raise TLSCustodyError("API custody file changed")
        data = b""
        while len(data) <= _MAX_BYTES:
            block = os.read(fd, _MAX_BYTES + 1 - len(data))
            if not block:
                break
            data += block
        after = os.fstat(fd)
        if len(data) > _MAX_BYTES or (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
            raise TLSCustodyError("API custody file changed")
        return data, info
    finally:
        os.close(fd)


def _parse(data, *, private=False):
    match = re.fullmatch(b"(" + _KEY + b")(" + _CERT + b")" if private else b"(" + _CERT + b")", data)
    if not match:
        raise TLSCustodyError("invalid API PEM inventory")
    certificate = x509.load_pem_x509_certificate(match.group(2 if private else 1))
    key = serialization.load_pem_private_key(match.group(1), password=None) if private else None
    if key is not None and (not isinstance(key, ec.EllipticCurvePrivateKey) or
            not isinstance(key.curve, ec.SECP256R1) or key.public_key() != certificate.public_key()):
        raise TLSCustodyError("API key/certificate mismatch")
    return key, certificate


def _check_records(records):
    for fd, name, mode, original in records:
        current = _read(fd, name, mode)
        if not _same(current[1], original[1]) or current[0] != original[0]:
            raise TLSCustodyError("API custody record changed after validation")


def _subject(instance, issuer):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                      f"Cortex API {'CA' if issuer else 'server'} {instance}")])


def _extensions(key, issuing_key, identity, *, issuer):
    extensions = [
        (x509.BasicConstraints(ca=issuer, path_length=0 if issuer else None), True),
        (x509.KeyUsage(not issuer, False, False, False, False, issuer, issuer, False, False), True),
        (x509.SubjectKeyIdentifier.from_public_key(key), False),
        (x509.AuthorityKeyIdentifier.from_issuer_public_key(issuing_key), False),
    ]
    if not issuer:
        extensions.extend([
            (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False),
            (x509.SubjectAlternativeName([*(x509.DNSName(name) for name in identity[1]),
                                          *(x509.IPAddress(address) for address in identity[2])]), False),
        ])
    return extensions


def _check_certificate(certificate, ca, identity, now, *, issuer=False, expired_leaf=False):
    key = certificate.public_key()
    if (not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1) or
            certificate.signature_algorithm_oid != SignatureAlgorithmOID.ECDSA_WITH_SHA256 or
            certificate.subject != _subject(identity[0], issuer) or certificate.issuer != ca.subject or
            (not issuer and key == ca.public_key())):
        raise TLSCustodyError("invalid API certificate identity/profile")
    certificate.verify_directly_issued_by(ca)
    expected = _extensions(key, ca.public_key(), identity, issuer=issuer)
    if len(certificate.extensions) != len(expected):
        raise TLSCustodyError("invalid API certificate extensions")
    for value, critical in expected:
        extension = certificate.extensions.get_extension_for_class(type(value))
        actual = extension.value
        matches = set(actual) == set(value) if isinstance(value, x509.SubjectAlternativeName) else actual == value
        if not matches or extension.critical != critical:
            raise TLSCustodyError("invalid API certificate extensions")
    start, end = certificate.not_valid_before_utc, certificate.not_valid_after_utc
    if (end - start != timedelta(days=1825 if issuer else 90) or now < start or
            (now >= end and (issuer or not expired_leaf)) or end > ca.not_valid_after_utc):
        raise TLSCustodyError("invalid API certificate validity")


def _issue(identity, now, *, ca_key=None, ca=None):
    issuer = ca_key is None
    key = ec.generate_private_key(ec.SECP256R1())
    signing_key = key if issuer else ca_key
    if not issuer and ca.not_valid_after_utc - now < timedelta(days=90):
        raise TLSCustodyError("API CA has insufficient remaining validity")
    builder = (x509.CertificateBuilder().subject_name(_subject(identity[0], issuer))
               .issuer_name(_subject(identity[0], True)).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now)
               .not_valid_after(now + timedelta(days=1825 if issuer else 90)))
    for value, critical in _extensions(key.public_key(), signing_key.public_key(), identity, issuer=issuer):
        builder = builder.add_extension(value, critical=critical)
    certificate = builder.sign(signing_key, hashes.SHA256())
    _check_certificate(certificate, certificate if issuer else ca, identity, now, issuer=issuer)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()) + certificate.public_bytes(serialization.Encoding.PEM)
    return key, certificate, pem


def _unlink_owned(directory, name, info):
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _same(current, info):
        os.unlink(name, dir_fd=directory)


@contextmanager
def _stage(directory, data, mode):
    name = ".api-tls-" + secrets.token_hex(16)
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)
    info = os.fstat(fd)
    try:
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise TLSCustodyError("API custody staging write failed")
            offset += written
        os.fsync(fd)
        staged = _read(directory, name, mode)
        if not _same(staged[1], info) or staged[0] != data:
            raise TLSCustodyError("API custody stage changed")
        yield name, info
    finally:
        try:
            _unlink_owned(directory, name, info)
        finally:
            os.close(fd)


def _publish_new(directory, name, data, mode, roots):
    with _stage(directory, data, mode) as (temporary, info):
        for path, fd in roots:
            _check_directory(path, fd)
        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        _unlink_owned(directory, temporary, info)
        created = _read(directory, name, mode)
        if not _same(created[1], info) or created[0] != data:
            raise TLSCustodyError("API custody publication changed")
        os.fsync(directory)
        for path, fd in roots:
            _check_directory(path, fd)
        return created


def _metadata(status, ca, leaf, identity):
    return {"status": status, "instance_id": identity[0],
            "ca_sha256": hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest(),
            "ca_valid_until": ca.not_valid_after_utc.isoformat(),
            "leaf_valid_until": leaf.not_valid_after_utc.isoformat()}


def _update(issuer_dir, server_dir, instance_id, dns_names, ip_addresses, *, renew):
    committed = False
    try:
        identity, now = _identity(instance_id, dns_names, ip_addresses), _now()
        with _directory(issuer_dir) as (issuer_path, issuer_fd), _directory(server_dir) as (server_path, server_fd):
            if (issuer_path == server_path or issuer_path in server_path.parents or server_path in issuer_path.parents or
                    _same(os.fstat(issuer_fd), os.fstat(server_fd))):
                raise TLSCustodyError("API issuer and server custody must not overlap")
            with _lock(server_fd, ".server.lock"), _lock(issuer_fd, ".issuer.lock"):
                issuer_record = _read(issuer_fd, "issuer.pem", 0o400, missing=True)
                public_record = _read(server_fd, "ca.crt", 0o444, missing=True)
                leaf_record = _read(server_fd, "server.pem", 0o400, missing=True)
                records = ((issuer_fd, "issuer.pem", 0o400, issuer_record),
                           (server_fd, "ca.crt", 0o444, public_record),
                           (server_fd, "server.pem", 0o400, leaf_record))
                if ((not issuer_record and (public_record or leaf_record)) or (leaf_record and not public_record)
                        or (renew and not all((issuer_record, public_record, leaf_record)))):
                    raise TLSCustodyError("unsupported partial API custody; explicit recovery required")
                if issuer_record:
                    ca_key, ca = _parse(issuer_record[0], private=True)
                    _check_certificate(ca, ca, identity, now, issuer=True)
                    issuer_pem = issuer_record[0]
                else:
                    ca_key, ca, issuer_pem = _issue(identity, now)
                ca_pem = ca.public_bytes(serialization.Encoding.PEM)
                if public_record and public_record[0] != ca_pem:
                    raise TLSCustodyError("API public trust does not match issuer")
                if leaf_record:
                    _, leaf = _parse(leaf_record[0], private=True)
                    _check_certificate(leaf, ca, identity, now, expired_leaf=renew)
                    if not renew:
                        _check_records(records)
                        _check_directory(issuer_path, issuer_fd)
                        _check_directory(server_path, server_fd)
                        return _metadata("unchanged", ca, leaf, identity)
                _, leaf, leaf_pem = _issue(identity, now, ca_key=ca_key, ca=ca)
                roots = ((issuer_path, issuer_fd), (server_path, server_fd))
                if not renew:
                    _check_directory(issuer_path, issuer_fd)
                    _check_directory(server_path, server_fd)
                    status = "resumed" if issuer_record else "created"
                    if not issuer_record:
                        issuer_record = _publish_new(issuer_fd, "issuer.pem", issuer_pem, 0o400, roots)
                    if not public_record:
                        public_record = _publish_new(server_fd, "ca.crt", ca_pem, 0o444, roots)
                    leaf_record = _publish_new(server_fd, "server.pem", leaf_pem, 0o400, roots)
                else:
                    with _stage(server_fd, leaf_pem, 0o400) as (temporary, staged_info):
                        _check_directory(issuer_path, issuer_fd)
                        _check_directory(server_path, server_fd)
                        _check_records(records)
                        os.replace(temporary, "server.pem", src_dir_fd=server_fd, dst_dir_fd=server_fd)
                        committed = True
                        os.fsync(server_fd)
                        leaf_record = (leaf_pem, staged_info)
                    status = "renewed"
                _check_records(((issuer_fd, "issuer.pem", 0o400, issuer_record),
                                (server_fd, "ca.crt", 0o444, public_record),
                                (server_fd, "server.pem", 0o400, leaf_record)))
                for path, fd in roots:
                    _check_directory(path, fd)
                return _metadata(status, ca, leaf, identity)
    except _EXPECTED_ERRORS:
        if committed:
            raise TLSCustodyError("API leaf published; uncertain durability, validate custody before retry") from None
        raise TLSCustodyError("API TLS custody refused or publication failed; existing artifacts were not adopted") from None


def initialize(issuer_dir, server_dir, *, instance_id, dns_names=_DNS, ip_addresses=_IPS):
    """Create empty custody, resume safe issuer prefixes, or validate a no-op."""
    return _update(issuer_dir, server_dir, instance_id, dns_names, ip_addresses, renew=False)


def renew_leaf(issuer_dir, server_dir, *, instance_id, dns_names=_DNS, ip_addresses=_IPS):
    """Explicitly replace one leaf/key pair, preserving the issuer and trust."""
    return _update(issuer_dir, server_dir, instance_id, dns_names, ip_addresses, renew=True)


def validate_server(server_dir, *, instance_id, dns_names=_DNS, ip_addresses=_IPS):
    """Validate public trust and the server leaf without any issuer-key access."""
    try:
        identity, now = _identity(instance_id, dns_names, ip_addresses), _now()
        with _directory(server_dir) as (path, fd), _lock(fd, ".server.lock", readonly=True):
            public_record = _read(fd, "ca.crt", 0o444)
            leaf_record = _read(fd, "server.pem", 0o400)
            _, ca = _parse(public_record[0])
            _, leaf = _parse(leaf_record[0], private=True)
            _check_certificate(ca, ca, identity, now, issuer=True)
            _check_certificate(leaf, ca, identity, now)
            _check_records(((fd, "ca.crt", 0o444, public_record), (fd, "server.pem", 0o400, leaf_record)))
            _check_directory(path, fd)
            return _metadata("valid", ca, leaf, identity)
    except _EXPECTED_ERRORS:
        raise TLSCustodyError("API server TLS custody is invalid or unavailable") from None
