"""Explicit per-installation client configuration (R23, plan W2 item 8).

Sources, in precedence order:
  1. an explicit configuration file (``CORTEX_V2_CLIENT_CONFIG`` or the
     ``path`` argument),
  2. explicit environment variables.

A configuration file must declare ``"profile": "v2"``. Legacy profile keys
(``x_project``, ``agent_name``, ``ctx1`` …) are rejected outright: the v2
client never infers or emulates the legacy profile, and never accepts a
shared administrator credential file. Token files must be owner-read-only.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ClientConfigError

CONFIG_ENV = "CORTEX_V2_CLIENT_CONFIG"
BASE_URL_ENV = "CORTEX_V2_BASE_URL"
TOKEN_ENV = "CORTEX_V2_TOKEN"
TOKEN_FILE_ENV = "CORTEX_V2_TOKEN_FILE"
SCOPE_ENV = "CORTEX_V2_SCOPE"
READ_SCOPES_ENV = "CORTEX_V2_READ_SCOPES"
INSTALLATION_LABEL_ENV = "CORTEX_V2_INSTALLATION_LABEL"
PRINCIPAL_LABEL_ENV = "CORTEX_V2_PRINCIPAL_LABEL"

LEGACY_CONFIG_KEYS = frozenset(
    {
        "x_project",
        "x_agent_name",
        "agent_name",
        "project",
        "ctx1",
        "ctx1_token",
        "legacy",
        "legacy_route",
        "legacy_routes",
        "omp",
        "omp_backend",
    }
)

FILE_KEYS = frozenset(
    {
        "profile",
        "base_url",
        "token",
        "token_file",
        "default_scope",
        "default_read_scopes",
        "installation_label",
        "principal_label",
    }
)


@dataclass(frozen=True, slots=True)
class ClientProfile:
    base_url: str
    token: str
    default_scope: str | None
    default_read_scopes: tuple[str, ...]
    installation_label: str | None
    principal_label: str | None
    source: str


def _read_token_file(path: str | Path) -> str:
    token_path = Path(path)
    if not token_path.is_file():
        raise ClientConfigError(f"token file not found: {token_path}")
    mode = token_path.stat().st_mode
    if mode & 0o077:
        raise ClientConfigError(
            f"token file {token_path} has permissive file mode "
            f"{stat.filemode(mode)}; a per-installation credential file must "
            "be owner-read-only (chmod 600)"
        )
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise ClientConfigError(f"token file {token_path} is empty")
    return token


def _validate_base_url(base_url: str) -> str:
    if not base_url.startswith(("http://", "https://")):
        raise ClientConfigError(
            "base_url must be an http(s) URL identifying this installation"
        )
    return base_url.rstrip("/")


def _split_scopes(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(part).strip() for part in raw]
    else:
        raise ClientConfigError("read scopes must be a list or comma string")
    return tuple(part for part in parts if part)


def _from_file(path: str | Path) -> ClientProfile:
    config_path = Path(path)
    if not config_path.is_file():
        raise ClientConfigError(f"client config file not found: {config_path}")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClientConfigError(
            f"client config {config_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ClientConfigError("client config must be a JSON object")
    legacy = LEGACY_CONFIG_KEYS.intersection(data)
    if legacy:
        raise ClientConfigError(
            "legacy profile configuration rejected: "
            f"{sorted(legacy)}; the v2 client speaks only the v2 profile "
            "(v2 bearer + X-Cortex-Scope), never ctx1/X-Project"
        )
    unknown = set(data) - FILE_KEYS
    if unknown:
        raise ClientConfigError(
            f"unknown client config keys: {sorted(unknown)}"
        )
    if data.get("profile") != "v2":
        raise ClientConfigError(
            'client config must declare "profile": "v2"; one profile per '
            "installation file, no inference"
        )
    base_url = data.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise ClientConfigError("client config requires base_url")
    token = data.get("token")
    token_file = data.get("token_file")
    if bool(token) == bool(token_file):
        raise ClientConfigError(
            "client config requires exactly one of token or token_file"
        )
    resolved_token = (
        token if isinstance(token, str) and token
        else _read_token_file(str(token_file))
    )
    default_scope = data.get("default_scope")
    if default_scope is not None and not isinstance(default_scope, str):
        raise ClientConfigError("default_scope must be a string")
    return ClientProfile(
        base_url=_validate_base_url(base_url),
        token=resolved_token,
        default_scope=default_scope or None,
        default_read_scopes=_split_scopes(data.get("default_read_scopes")),
        installation_label=data.get("installation_label"),
        principal_label=data.get("principal_label"),
        source=str(config_path),
    )


def _from_env(env: Mapping[str, str]) -> ClientProfile:
    base_url = env.get(BASE_URL_ENV, "")
    if not base_url:
        raise ClientConfigError(
            "no client configuration found: provide "
            f"{CONFIG_ENV} (file) or {BASE_URL_ENV} plus {TOKEN_ENV}/"
            f"{TOKEN_FILE_ENV}"
        )
    token = env.get(TOKEN_ENV, "")
    token_file = env.get(TOKEN_FILE_ENV, "")
    if bool(token) == bool(token_file):
        raise ClientConfigError(
            f"provide exactly one of {TOKEN_ENV} or {TOKEN_FILE_ENV}"
        )
    resolved_token = token if token else _read_token_file(token_file)
    return ClientProfile(
        base_url=_validate_base_url(base_url),
        token=resolved_token,
        default_scope=env.get(SCOPE_ENV) or None,
        default_read_scopes=_split_scopes(env.get(READ_SCOPES_ENV)),
        installation_label=env.get(INSTALLATION_LABEL_ENV),
        principal_label=env.get(PRINCIPAL_LABEL_ENV),
        source="environment",
    )


def load_client_profile(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> ClientProfile:
    environment: Mapping[str, str] = os.environ if env is None else env
    if path is not None:
        return _from_file(path)
    config_env = environment.get(CONFIG_ENV, "")
    if config_env:
        return _from_file(config_env)
    return _from_env(environment)


def profile_from_credentials(
    base_url: str,
    token: str,
    *,
    scope: str | None = None,
    read_scopes: tuple[str, ...] = (),
    installation_label: str | None = None,
    principal_label: str | None = None,
) -> ClientProfile:
    """Build a profile from already-resolved per-request credentials
    (used by the streamable-HTTP MCP server; never from ambient env)."""
    return ClientProfile(
        base_url=_validate_base_url(base_url),
        token=token,
        default_scope=scope,
        default_read_scopes=tuple(read_scopes),
        installation_label=installation_label,
        principal_label=principal_label,
        source="per-request-credentials",
    )
