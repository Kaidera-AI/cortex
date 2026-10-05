"""Select one installation/identity; credential files and token env are not profiles."""

from __future__ import annotations

import ipaddress
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ClientConfigError
from .key_store import KeyStore, KeyStoreError
from .member_reader import MemberKeyReader


URL_ENV = "CORTEX_URL"
CI_KEY_ENV = "CORTEX_KEY"
LEGACY_CREDENTIAL_ENV = frozenset({
    "CORTEX_V2_CLIENT_CONFIG", "CORTEX_V2_BASE_URL", "CORTEX_V2_TOKEN",
    "CORTEX_V2_TOKEN_FILE", "CORTEX_V2_SCOPE", "CORTEX_V2_READ_SCOPES",
    "CORTEX_V2_INSTALLATION_LABEL", "CORTEX_V2_PRINCIPAL_LABEL",
})
CONNECTION_KEYS = frozenset({
    "profile", "base_url", "installation", "project", "name",
    "default_scope", "default_read_scopes",
    "project_root",
})


@dataclass(frozen=True, slots=True)
class ClientProfile:
    base_url: str
    token: str = field(repr=False)
    default_scope: str | None
    default_read_scopes: tuple[str, ...]
    installation_label: str | None
    principal_label: str | None
    source: str
    credential_store: KeyStore | None = None
    credential_project: str | None = None
    credential_name: str | None = None
    member_reader: MemberKeyReader | None = field(default=None, repr=False)


def _validate_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ClientConfigError("CORTEX_URL must be a valid API origin") from exc
    if (parsed.scheme not in ("http", "https") or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ClientConfigError("CORTEX_URL must be an API origin without credentials or path")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if parsed.scheme != "https" and not loopback:
        raise ClientConfigError("non-loopback Cortex URLs require HTTPS")
    try:
        parsed.port
    except ValueError as exc:
        raise ClientConfigError("CORTEX_URL has an invalid port") from exc
    return value.rstrip("/")


def _connection(path: str | Path | None) -> tuple[dict[str, Any], str]:
    chosen = Path(path) if path is not None else Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ) / "cortex" / "connection.json"
    if path is None and not chosen.exists() and not chosen.is_symlink():
        return {}, "environment"
    try:
        fd = os.open(chosen, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ClientConfigError("connection profile must be an owner-only regular file")
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClientConfigError("cannot read connection profile") from exc
    if not isinstance(data, dict) or set(data) - CONNECTION_KEYS or data.get("profile") != "v2":
        raise ClientConfigError("connection profile must be v2 and contain no credential fields")
    for field in ("base_url", "installation", "project", "name", "default_scope", "project_root"):
        if field in data and not isinstance(data[field], str):
            raise ClientConfigError(f"connection profile field {field} must be text")
    return data, str(chosen)


def _split_scopes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return tuple(part.strip() for part in value if part.strip())
    raise ClientConfigError("read scopes must be a list of names")


def _require_url(data: Mapping[str, Any], environment: Mapping[str, str]) -> str:
    url = environment.get(URL_ENV) or data.get("base_url")
    if not url:
        raise ClientConfigError("provide CORTEX_URL or a non-secret connection profile")
    return _validate_base_url(url)


def load_connection_url(
    path: str | Path | None = None, *, env: Mapping[str, str] | None = None,
) -> str:
    environment = os.environ if env is None else env
    if LEGACY_CREDENTIAL_ENV.intersection(environment):
        raise ClientConfigError("obsolete credential environment is prohibited")
    data, _ = _connection(path) if env is None or path is not None else ({}, "environment")
    return _require_url(data, environment)


def load_client_profile(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    store: KeyStore | None = None,
    installation: str | None = None,
    project: str | None = None,
    name: str | None = None,
) -> ClientProfile:
    environment = os.environ if env is None else env
    forbidden = LEGACY_CREDENTIAL_ENV.intersection(environment)
    if forbidden:
        raise ClientConfigError("obsolete credential environment is prohibited; use CORTEX_URL and private key store")
    data, source = _connection(path) if env is None or path is not None else ({}, "environment")
    base_url = _require_url(data, environment)
    selected_installation = installation or data.get("installation")
    selected_project = project or data.get("project") or "@installation"
    selected_name = name or data.get("name") or "owner"
    ci_key = environment.get(CI_KEY_ENV)
    if ci_key is not None:
        if environment.get("CI", "").lower() not in ("true", "1"):
            raise ClientConfigError("CORTEX_KEY is accepted only in explicit CI")
        if not ci_key:
            raise ClientConfigError("explicit CI CORTEX_KEY must not be empty")
        token = ci_key
        store = None
    else:
        if store is None:
            if not selected_installation:
                raise ClientConfigError("select --installation to load the private credential")
            try:
                store = KeyStore(selected_installation)
            except KeyStoreError as exc:
                raise ClientConfigError(str(exc)) from exc
        try:
            token = store.get(selected_project, selected_name)
        except KeyStoreError as exc:
            raise ClientConfigError(str(exc)) from exc
        if not token:
            raise ClientConfigError("credential not found; ask the project lead/owner for enrollment")
        selected_installation = store.installation
    return ClientProfile(
        base_url=base_url,
        token=token,
        default_scope=data.get("default_scope") or (selected_project if selected_project != "@installation" else None),
        default_read_scopes=_split_scopes(data.get("default_read_scopes")),
        installation_label=selected_installation,
        principal_label=selected_name,
        source=source,
        credential_store=store,
        credential_project=selected_project if store is not None else None,
        credential_name=selected_name if store is not None else None,
    )


def profile_from_credentials(
    base_url: str,
    token: str,
    *,
    scope: str | None = None,
    read_scopes: tuple[str, ...] = (),
    installation_label: str | None = None,
    principal_label: str | None = None,
) -> ClientProfile:
    """Explicit per-request credentials, never ambient key-store fallback."""
    return ClientProfile(
        base_url=_validate_base_url(base_url),
        token=token,
        default_scope=scope,
        default_read_scopes=tuple(read_scopes),
        installation_label=installation_label,
        principal_label=principal_label,
        source="per-request-credentials",
    )


def load_member_profile(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    installation: str | None = None,
    project: str | None = None,
    name: str | None = None,
    project_root: Path | None = None,
) -> ClientProfile:
    """Select a project member without reading or retaining its bearer."""
    environment = os.environ if env is None else env
    if LEGACY_CREDENTIAL_ENV.intersection(environment) or CI_KEY_ENV in environment:
        raise ClientConfigError("member clients require their selected private reader; ambient keys are prohibited")
    data, source = _connection(path) if env is None or path is not None else ({}, "environment")
    base_url = _require_url(data, environment)
    installation = installation or data.get("installation")
    project = project or data.get("project")
    name = name or data.get("name")
    if project_root is None and data.get("project_root"):
        project_root = Path(data["project_root"])
    if not installation or not project or not name or project_root is None:
        raise ClientConfigError("member clients require installation, project, name and physical project_root")
    read_scopes = _split_scopes(data.get("default_read_scopes"))
    if data.get("default_scope", project) != project or any(value != project for value in read_scopes):
        raise ClientConfigError("member profile scope must match its selected project")
    try:
        reader = MemberKeyReader(installation=installation, project=project,
                                 name=name, project_root=project_root)
    except KeyStoreError as exc:
        raise ClientConfigError(str(exc)) from None
    return ClientProfile(
        base_url=base_url, token="", default_scope=project,
        default_read_scopes=read_scopes, installation_label=installation,
        principal_label=name, source=source, member_reader=reader,
    )
