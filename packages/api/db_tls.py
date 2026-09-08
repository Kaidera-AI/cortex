"""PostgreSQL connection options shared by every Cortex API connection.

Standalone uses PostgreSQL's native ``cert`` authentication. Embedded deployments
keep their existing DSN options when CORTEX_DB_AUTH is unset or ``embedded``.
"""

from __future__ import annotations

import os
from pathlib import Path
import ssl
import stat
from urllib.parse import parse_qsl, unquote, urlsplit


def _reject_password() -> str:
    raise RuntimeError("CORTEX_DB_AUTH=certificate refuses password authentication")


def connection_kwargs(dsn: str, role: str = "app") -> dict:
    """Return verified TLS options, or preserve the explicit embedded DSN.

    Build a new context for each connection/pool creation so a reconnect after an
    operator-managed leaf rotation sees the current certificate. No validation
    error is caught: missing custody prevents startup rather than weakening TLS.
    """
    mode = os.getenv("CORTEX_DB_AUTH", "embedded").strip().lower()
    if mode == "embedded":
        return {}
    if mode != "certificate":
        raise RuntimeError("CORTEX_DB_AUTH must be embedded or certificate")
    if role not in {"app", "admin"}:
        raise ValueError("database certificate role must be app or admin")

    parsed = urlsplit(dsn)
    expected_user = "cortex_app" if role == "app" else "postgres"
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or "/" in unquote(parsed.hostname)
        or unquote(parsed.username or "") != expected_user
    ):
        raise RuntimeError(f"certificate {role} DSN must name TCP host and user {expected_user}")
    if parsed.password is not None:
        raise RuntimeError("certificate DSNs must not contain a password")
    # The context below owns all TLS options. Conflicting URI options are likely
    # deployment mistakes, and query-form credentials must not bypass the check.
    forbidden = {"user", "password", "passfile", "host", "hostaddr"}
    if any(key.lower().startswith("ssl") or key.lower() in forbidden
           for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise RuntimeError("certificate DSN must not override TLS, host, or credentials")

    prefix = f"CORTEX_DB_TLS_{role.upper()}"
    directory = Path("/tls") / role
    ca = Path(os.getenv(f"{prefix}_CA", os.getenv("CORTEX_DB_TLS_CA", str(directory / "ca.crt"))))
    cert = Path(os.getenv(f"{prefix}_CERT", str(directory / "tls.crt")))
    key = Path(os.getenv(f"{prefix}_KEY", str(directory / "tls.key")))
    for path in (ca, cert, key):
        if not path.is_file():
            raise RuntimeError(f"database TLS file is missing or not regular: {path}")
    if not stat.S_ISREG(key.stat().st_mode) or key.stat().st_mode & 0o077:
        raise RuntimeError("database TLS private key must be accessible only to its owner")

    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(str(cert), str(key), password=_reject_password)
    # A callback also blocks accidental PGPASSWORD/.pgpass use if a server is
    # misconfigured to ask for a password instead of performing cert auth.
    return {"ssl": context, "password": _reject_password, "passfile": os.devnull}
