"""Fail-closed Processing configuration (R24/R25).

:func:`parse_settings` is a pure validated function over an env mapping — it
never touches process state, so tests drive it directly.
:meth:`ProcessingSettings.from_env` additionally resolves every secret-file
reference through :func:`cortex_v2.config.read_secret_path` so the worker
fails at boot instead of mid-attempt; resolved bytes are validated and then
discarded, never retained.

Secret *values* are never stored or rendered anywhere in this module:
credentials and DSNs are referenced by env/secret-file NAME only, and no
exception message or ``repr`` may echo a value. Every failure raises
:exc:`cortex_v2.config.ConfigurationError` naming the offending setting.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from urllib.parse import urlsplit

from .. import config
from ..config import ConfigurationError
from .contracts import EXECUTOR_ROLES, WORKER_ROLES, ParserLimits
from .providers import EgressPolicy, is_restricted_address

#: Provider roles a deployment can configure (R24).
PROVIDER_ROLES = ("embedding", "rerank", "analysis")

#: Canonical provider families, plus ``none`` for an unconfigured role. The
#: family strings double as the ``provider_id`` prefixes the embedding
#: adapters expose; ``embedding.PROVIDER_FAMILIES`` re-exports the three.
PROVIDER_KIND_HASH_LOCAL = "hash-local"
PROVIDER_KIND_OPENAI_COMPATIBLE = "openai-compatible"
PROVIDER_KIND_OLLAMA = "ollama"
PROVIDER_KIND_NONE = "none"
PROVIDER_KINDS = (
    PROVIDER_KIND_HASH_LOCAL,
    PROVIDER_KIND_OPENAI_COMPATIBLE,
    PROVIDER_KIND_OLLAMA,
    PROVIDER_KIND_NONE,
)
#: Kinds that talk to an HTTP endpoint (remote over the internet, or a local
#: ollama server) and therefore require base_url + model.
SERVER_KINDS = (PROVIDER_KIND_OPENAI_COMPATIBLE, PROVIDER_KIND_OLLAMA)
REMOTE_KINDS = (PROVIDER_KIND_OPENAI_COMPATIBLE,)
#: Kinds that must never carry a credential reference.
OFFLINE_KINDS = (PROVIDER_KIND_HASH_LOCAL, PROVIDER_KIND_NONE)

ROUTING_LOCAL_ONLY = "local_only"
ROUTING_REMOTE_ALLOWED = "remote_allowed"
ROUTING_DISABLED = "disabled"
ROUTING_POLICIES = (ROUTING_LOCAL_ONLY, ROUTING_REMOTE_ALLOWED, ROUTING_DISABLED)

MODEL_STATUS_INSTALLED = "installed"
MODEL_STATUS_ACTIVATED = "activated"
MODEL_STATUSES = ("declared", MODEL_STATUS_INSTALLED, MODEL_STATUS_ACTIVATED, "retired")
#: Manifest statuses a provider may actually execute against.
USABLE_MODEL_STATUSES = (MODEL_STATUS_INSTALLED, MODEL_STATUS_ACTIVATED)

_ENV = "CORTEX_V2_"
_WORKER = f"{_ENV}WORKER_"
_PROVIDER = f"{_ENV}PROVIDER_"
_INFERENCE = f"{_ENV}INFERENCE_"
_PARSER = f"{_ENV}PARSER_"

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_HOST_ENTRY = re.compile(r"^[a-z0-9]([a-z0-9._:-]*[a-z0-9])?$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,255}$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Worker loop tuning plus name-only secret references."""

    role: str
    worker_id: str
    database_url_file: str
    principal_id_file: str
    executor_roles: tuple[str, ...]
    handler_modules: tuple[str, ...]
    poll_interval_seconds: float
    lease_seconds: int
    heartbeat_interval_seconds: float
    attempt_deadline_seconds: float
    max_concurrent_attempts: int
    thread_pool_size: int
    backoff_base_seconds: float
    backoff_cap_seconds: float
    queue_admission_limit: int
    reap_interval_seconds: float
    blob_root: str | None


@dataclass(frozen=True, slots=True)
class ProviderRoleSettings:
    """One provider role; credentials are references, never values."""

    role: str
    enabled: bool = False
    provider_kind: str = PROVIDER_KIND_NONE
    base_url: str | None = None
    model: str = ""
    credential_env: str | None = None
    credential_file: str | None = None
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    max_batch: int = 64
    max_input_chars: int = 65_536
    egress_allowlist: tuple[str, ...] = ()
    allow_private_for: tuple[str, ...] = ()

    def egress_policy(self) -> EgressPolicy:
        return EgressPolicy(
            allowlist=self.egress_allowlist,
            allow_private_for=self.allow_private_for,
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class LocalModelManifest:
    """One locally declared model (R25); ``dimensions`` may be unpinned."""

    model_id: str
    digest: str
    license: str
    platform: str
    status: str
    dimensions: int | None


@dataclass(frozen=True, slots=True)
class LocalInferenceSettings:
    """Local inference controls and the model manifest table (R25)."""

    routing_policy: str = ROUTING_DISABLED
    models: tuple[LocalModelManifest, ...] = ()
    max_resident_models: int = 2
    concurrency_cpu: int = 4
    concurrency_gpu: int = 0
    batch_limit: int = 32
    idle_unload_seconds: int = 300
    offline: bool = False

    def manifest_for(self, model_id: str) -> LocalModelManifest | None:
        for manifest in self.models:
            if manifest.model_id == model_id:
                return manifest
        return None

    def has_installed_model(self, model_id: str) -> bool:
        manifest = self.manifest_for(model_id)
        return manifest is not None and manifest.status in USABLE_MODEL_STATUSES


@dataclass(frozen=True, slots=True)
class ProcessingSettings:
    """Fully validated Processing configuration for one worker process."""

    worker: WorkerSettings
    providers: tuple[ProviderRoleSettings, ...]
    local: LocalInferenceSettings
    parser_limits: ParserLimits

    def provider_for_role(self, role: str) -> ProviderRoleSettings | None:
        for settings in self.providers:
            if settings.role == role:
                return settings
        return None

    def secret_file_references(self) -> tuple[str, ...]:
        """Env names whose values are secret-file paths resolved at boot."""
        references = [
            self.worker.database_url_file,
            self.worker.principal_id_file,
        ]
        for role in PROVIDER_ROLES:
            provider = self.provider_for_role(role)
            if provider is not None and provider.enabled and provider.credential_file:
                references.append(provider.credential_file)
        return tuple(references)

    @classmethod
    def from_env(cls) -> ProcessingSettings:
        settings = parse_settings(os.environ)
        for reference in settings.secret_file_references():
            # Fail at boot when a secret file is missing or unreadable; the
            # resolved bytes are deliberately not retained (name-only refs).
            config.read_secret_path(reference)
        return settings


# ---------------------------------------------------------------------------
# Validation primitives: every error names the setting, never a value that
# could be a secret.
# ---------------------------------------------------------------------------


def _fail(key: str, problem: str) -> ConfigurationError:
    return ConfigurationError(f"invalid processing setting {key}: {problem}")


def _missing(key: str, note: str = "") -> ConfigurationError:
    suffix = f" ({note})" if note else ""
    return ConfigurationError(
        f"required processing setting is missing: {key}{suffix}"
    )


def _raw(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _required_str(env: Mapping[str, str], key: str) -> str:
    value = _raw(env, key)
    if value is None:
        raise _missing(key)
    return value


def _optional_str(env: Mapping[str, str], key: str) -> str | None:
    return _raw(env, key)


def _number(
    env: Mapping[str, str],
    key: str,
    default: float | int,
    *,
    low: float,
    high: float,
    integer: bool,
) -> float | int:
    raw = _raw(env, key)
    if raw is None:
        return default
    try:
        value: float | int = int(raw) if integer else float(raw)
    except ValueError:
        kind = "an integer" if integer else "a number"
        raise _fail(key, f"expected {kind}") from None
    if integer:
        in_range = low <= value <= high
    else:
        in_range = math.isfinite(value) and low <= value <= high
    if not in_range:
        raise _fail(key, f"value must be within {low}..{high}")
    return value


def _boolean(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _raw(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise _fail(key, "expected a boolean (true/false)")


def _required_enum(env: Mapping[str, str], key: str, allowed: tuple[str, ...]) -> str:
    value = _required_str(env, key)
    if value not in allowed:
        raise _fail(key, f"unknown value; allowed: {', '.join(allowed)}")
    return value


def _env_name(env: Mapping[str, str], key: str) -> str | None:
    value = _raw(env, key)
    if value is None:
        return None
    if not _ENV_NAME.match(value):
        raise _fail(key, "value must be an environment variable name")
    return value


def _host_list(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw = _raw(env, key)
    if raw is None:
        return ()
    entries: list[str] = []
    for entry in raw.split(","):
        host = entry.strip().lower()
        if not host:
            raise _fail(key, "empty host entry")
        if not _HOST_ENTRY.match(host):
            raise _fail(key, "entries must be bare hostnames or IP literals")
        entries.append(host)
    return tuple(entries)


def _modules(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw = _raw(env, key)
    if raw is None:
        return ()
    modules: list[str] = []
    for entry in raw.split(","):
        name = entry.strip()
        if not name:
            raise _fail(key, "empty module entry")
        if not _MODULE_NAME.match(name):
            raise _fail(key, f"'{name}' is not a dotted module name")
        modules.append(name)
    return tuple(modules)


def _secret_reference(env: Mapping[str, str], key: str, default: str) -> str:
    name = _raw(env, key) or default
    if not _ENV_NAME.match(name):
        raise _fail(key, "value must be an environment variable name")
    if _raw(env, name) is None:
        raise ConfigurationError(
            f"invalid processing setting {key}: the referenced variable "
            f"{name} is missing from the environment"
        )
    return name


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _worker(env: Mapping[str, str]) -> WorkerSettings:
    role = _required_enum(env, f"{_WORKER}ROLE", WORKER_ROLES)
    worker_id = _required_str(env, f"{_WORKER}ID")
    if not _WORKER_ID.match(worker_id):
        raise _fail(f"{_WORKER}ID", "expected 1-128 chars of [A-Za-z0-9._-]")
    executor_roles = _executor_roles(env, f"{_WORKER}EXECUTOR_ROLES")
    handler_modules = _modules(env, f"{_WORKER}HANDLER_MODULES")
    poll = _number(
        env, f"{_WORKER}POLL_INTERVAL_SECONDS", 2.0, low=0.05, high=300.0, integer=False
    )
    lease = _number(env, f"{_WORKER}LEASE_SECONDS", 120, low=1, high=3600, integer=True)
    heartbeat = _number(
        env,
        f"{_WORKER}HEARTBEAT_INTERVAL_SECONDS",
        15.0,
        low=0.5,
        high=300.0,
        integer=False,
    )
    if heartbeat * 2.0 > lease:
        raise _fail(
            f"{_WORKER}HEARTBEAT_INTERVAL_SECONDS",
            f"must renew at least twice per lease ({lease}s)",
        )
    attempt_deadline = _number(
        env,
        f"{_WORKER}ATTEMPT_DEADLINE_SECONDS",
        900.0,
        low=1.0,
        high=21600.0,
        integer=False,
    )
    concurrent = _number(
        env, f"{_WORKER}MAX_CONCURRENT_ATTEMPTS", 4, low=1, high=64, integer=True
    )
    pool = _number(env, f"{_WORKER}THREAD_POOL_SIZE", 8, low=1, high=128, integer=True)
    backoff_base = _number(
        env, f"{_WORKER}BACKOFF_BASE_SECONDS", 2.0, low=0.05, high=300.0, integer=False
    )
    backoff_cap = _number(
        env, f"{_WORKER}BACKOFF_CAP_SECONDS", 300.0, low=1.0, high=3600.0, integer=False
    )
    if backoff_cap < backoff_base:
        raise _fail(
            f"{_WORKER}BACKOFF_CAP_SECONDS",
            "must be at least CORTEX_V2_WORKER_BACKOFF_BASE_SECONDS",
        )
    admission = _number(
        env, f"{_WORKER}QUEUE_ADMISSION_LIMIT", 1000, low=1, high=100_000, integer=True
    )
    reap = _number(
        env,
        f"{_WORKER}REAP_INTERVAL_SECONDS",
        30.0,
        low=0.5,
        high=3600.0,
        integer=False,
    )
    database_url_file = _secret_reference(
        env, f"{_WORKER}DATABASE_URL_FILE_ENV", "CORTEX_V2_DATABASE_URL_FILE"
    )
    principal_id_file = _secret_reference(
        env, f"{_WORKER}PRINCIPAL_ID_FILE_ENV", "CORTEX_V2_WORKER_PRINCIPAL_ID_FILE"
    )
    blob_root = _optional_str(env, f"{_WORKER}BLOB_ROOT")
    return WorkerSettings(
        role=role,
        worker_id=worker_id,
        database_url_file=database_url_file,
        principal_id_file=principal_id_file,
        executor_roles=executor_roles,
        handler_modules=handler_modules,
        poll_interval_seconds=poll,
        lease_seconds=lease,
        heartbeat_interval_seconds=heartbeat,
        attempt_deadline_seconds=attempt_deadline,
        max_concurrent_attempts=concurrent,
        thread_pool_size=pool,
        backoff_base_seconds=backoff_base,
        backoff_cap_seconds=backoff_cap,
        queue_admission_limit=admission,
        reap_interval_seconds=reap,
        blob_root=blob_root,
    )


def _executor_roles(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    raw = _raw(env, key)
    if raw is None:
        return ("core",)
    roles: list[str] = []
    for entry in raw.split(","):
        role = entry.strip()
        if role not in EXECUTOR_ROLES:
            allowed = ", ".join(EXECUTOR_ROLES)
            raise _fail(key, f"unknown executor role; allowed: {allowed}")
        if role not in roles:
            roles.append(role)
    if not roles:
        raise _fail(key, "at least one executor role is required")
    return tuple(roles)


def _parser_limits(env: Mapping[str, str]) -> ParserLimits:
    overrides: dict[str, int] = {}
    for limit_field in fields(ParserLimits):
        key = f"{_PARSER}{limit_field.name.upper()}"
        raw = _raw(env, key)
        if raw is None:
            continue
        try:
            overrides[limit_field.name] = int(raw)
        except ValueError:
            raise _fail(key, "expected an integer") from None
    limits = replace(ParserLimits(), **overrides)
    problems = limits.violations()
    if problems:
        keys = ", ".join(f"{_PARSER}{name.upper()}" for name in problems)
        raise ConfigurationError(
            f"invalid processing setting(s) outside the contract bounds: {keys}"
        )
    return limits


def _role(env: Mapping[str, str], role: str) -> ProviderRoleSettings:
    prefix = f"{_PROVIDER}{role.upper()}_"
    enabled = _boolean(env, f"{prefix}ENABLED", False)
    kind = _raw(env, f"{prefix}KIND") or PROVIDER_KIND_NONE
    if kind not in PROVIDER_KINDS:
        allowed = ", ".join(PROVIDER_KINDS)
        raise _fail(f"{prefix}KIND", f"unknown provider_kind; allowed: {allowed}")
    if enabled and kind == PROVIDER_KIND_NONE:
        raise _missing(f"{prefix}KIND", "an enabled role requires a provider kind")
    base_url = _optional_str(env, f"{prefix}BASE_URL")
    model = _raw(env, f"{prefix}MODEL") or ""
    if model and not _MODEL_ID.match(model):
        raise _fail(f"{prefix}MODEL", "expected a model id without whitespace")
    credential_env = _env_name(env, f"{prefix}CREDENTIAL_ENV")
    credential_file = _env_name(env, f"{prefix}CREDENTIAL_FILE")
    connect_timeout = _number(
        env, f"{prefix}CONNECT_TIMEOUT_SECONDS", 5.0, low=0.05, high=60.0, integer=False
    )
    read_timeout = _number(
        env, f"{prefix}READ_TIMEOUT_SECONDS", 30.0, low=0.05, high=600.0, integer=False
    )
    max_batch = _number(env, f"{prefix}MAX_BATCH", 64, low=1, high=2048, integer=True)
    max_input_chars = _number(
        env, f"{prefix}MAX_INPUT_CHARS", 65_536, low=1, high=8_000_000, integer=True
    )
    allowlist = _host_list(env, f"{prefix}EGRESS_ALLOWLIST")
    allow_private_for = _host_list(env, f"{prefix}ALLOW_PRIVATE_FOR")

    if credential_env and credential_file:
        raise ConfigurationError(
            f"invalid processing setting: {prefix}CREDENTIAL_ENV and "
            f"{prefix}CREDENTIAL_FILE are mutually exclusive"
        )
    if kind in OFFLINE_KINDS and (credential_env or credential_file):
        raise ConfigurationError(
            f"invalid processing setting: {prefix}CREDENTIAL_ENV and "
            f"{prefix}CREDENTIAL_FILE must stay empty for the offline "
            f"'{kind}' kind"
        )
    if enabled and kind in SERVER_KINDS and not (credential_env or credential_file):
        raise _missing(
            f"{prefix}CREDENTIAL_ENV",
            f"an enabled '{kind}' role also accepts {prefix}CREDENTIAL_FILE",
        )
    if kind in SERVER_KINDS:
        if base_url is None:
            raise _missing(f"{prefix}BASE_URL", f"'{kind}' role requires an endpoint")
        _validate_base_url(prefix, kind, base_url, allowlist, allow_private_for)
        if not model:
            raise _missing(f"{prefix}MODEL", f"'{kind}' role requires a model id")
    return ProviderRoleSettings(
        role=role,
        enabled=enabled,
        provider_kind=kind,
        base_url=base_url,
        model=model,
        credential_env=credential_env,
        credential_file=credential_file,
        connect_timeout_seconds=connect_timeout,
        read_timeout_seconds=read_timeout,
        max_batch=max_batch,
        max_input_chars=max_input_chars,
        egress_allowlist=allowlist,
        allow_private_for=allow_private_for,
    )


def _validate_base_url(
    prefix: str,
    kind: str,
    base_url: str,
    allowlist: tuple[str, ...],
    allow_private_for: tuple[str, ...],
) -> None:
    """Mirror :meth:`EgressPolicy.check` statically; never echo the full URL
    because it could embed credentials."""
    key = f"{prefix}BASE_URL"
    parts = urlsplit(base_url)
    if parts.username or parts.password:
        raise _fail(key, "base_url must not embed credentials")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise _fail(key, "base_url has no host")
    if kind in REMOTE_KINDS and scheme != "https":
        raise _fail(
            key,
            "a remote provider role requires an https base_url "
            f"(scheme '{scheme}')",
        )
    if kind not in REMOTE_KINDS and scheme not in ("https", "http"):
        raise _fail(key, f"scheme '{scheme}' is not supported")
    if host not in allowlist:
        raise _fail(key, f"host '{host}' is missing from {prefix}EGRESS_ALLOWLIST")
    try:
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = (
            ipaddress.ip_address(host)
        )
    except ValueError:
        literal = None
    if scheme == "http" and host not in allow_private_for:
        raise _fail(
            key, f"an http base_url requires '{host}' in {prefix}ALLOW_PRIVATE_FOR"
        )
    if literal is not None and is_restricted_address(literal):
        if host not in allow_private_for:
            raise _fail(
                key,
                f"host '{host}' is a restricted address and requires "
                f"{prefix}ALLOW_PRIVATE_FOR",
            )


def _manifest_field(key: str, position: int, entry: dict, name: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _fail(key, f"manifest {position} is missing '{name}'")
    return value.strip()


def _manifest(key: str, position: int, entry: object) -> LocalModelManifest:
    if not isinstance(entry, dict):
        raise _fail(key, f"manifest {position} is not a JSON object")
    values = {
        name: _manifest_field(key, position, entry, name)
        for name in ("model_id", "digest", "license", "platform", "status")
    }
    status = values["status"].lower()
    if status not in MODEL_STATUSES:
        raise _fail(
            key,
            f"manifest {position} has an unknown status; "
            f"allowed: {', '.join(MODEL_STATUSES)}",
        )
    dimensions: int | None = None
    raw_dimensions = entry.get("dimensions")
    if raw_dimensions is not None:
        if isinstance(raw_dimensions, bool) or not isinstance(raw_dimensions, int):
            raise _fail(
                key, f"manifest {position} 'dimensions' must be an integer or null"
            )
        if not 1 <= raw_dimensions <= 4096:
            raise _fail(key, f"manifest {position} 'dimensions' must be within 1..4096")
        dimensions = raw_dimensions
    return LocalModelManifest(
        model_id=values["model_id"],
        digest=values["digest"],
        license=values["license"],
        platform=values["platform"],
        status=status,
        dimensions=dimensions,
    )


def _manifests(env: Mapping[str, str], key: str) -> tuple[LocalModelManifest, ...]:
    raw = _raw(env, key)
    if raw is None:
        return ()
    try:
        document = json.loads(raw)
    except ValueError:
        raise _fail(key, "value is not valid JSON") from None
    if not isinstance(document, list):
        raise _fail(key, "expected a JSON array of model manifests")
    manifests: list[LocalModelManifest] = []
    seen: set[str] = set()
    for position, entry in enumerate(document):
        manifest = _manifest(key, position, entry)
        if manifest.model_id in seen:
            raise _fail(key, f"duplicate model_id '{manifest.model_id}'")
        seen.add(manifest.model_id)
        manifests.append(manifest)
    return tuple(manifests)


def _local(env: Mapping[str, str]) -> LocalInferenceSettings:
    policy = _required_enum(env, f"{_INFERENCE}ROUTING_POLICY", ROUTING_POLICIES)
    models = _manifests(env, f"{_INFERENCE}MODELS")
    if policy == ROUTING_LOCAL_ONLY and not any(
        manifest.status in USABLE_MODEL_STATUSES for manifest in models
    ):
        raise ConfigurationError(
            f"invalid processing setting {_INFERENCE}ROUTING_POLICY: local_only "
            f"requires at least one installed/activated manifest in {_INFERENCE}MODELS"
        )
    max_resident = _number(
        env, f"{_INFERENCE}MAX_RESIDENT_MODELS", 2, low=1, high=64, integer=True
    )
    cpu = _number(
        env, f"{_INFERENCE}CONCURRENCY_CPU", 4, low=1, high=1024, integer=True
    )
    gpu = _number(env, f"{_INFERENCE}CONCURRENCY_GPU", 0, low=0, high=256, integer=True)
    batch_limit = _number(
        env, f"{_INFERENCE}BATCH_LIMIT", 32, low=1, high=2048, integer=True
    )
    idle_unload = _number(
        env, f"{_INFERENCE}IDLE_UNLOAD_SECONDS", 300, low=1, high=86_400, integer=True
    )
    offline = _boolean(env, f"{_INFERENCE}OFFLINE", False)
    return LocalInferenceSettings(
        routing_policy=policy,
        models=models,
        max_resident_models=max_resident,
        concurrency_cpu=cpu,
        concurrency_gpu=gpu,
        batch_limit=batch_limit,
        idle_unload_seconds=idle_unload,
        offline=offline,
    )


def parse_settings(env: Mapping[str, str]) -> ProcessingSettings:
    """Pure validated parse; every failure names the offending setting."""
    return ProcessingSettings(
        worker=_worker(env),
        providers=tuple(_role(env, role) for role in PROVIDER_ROLES),
        local=_local(env),
        parser_limits=_parser_limits(env),
    )


__all__ = [
    "MODEL_STATUSES",
    "MODEL_STATUS_ACTIVATED",
    "MODEL_STATUS_INSTALLED",
    "PROVIDER_KINDS",
    "PROVIDER_KIND_HASH_LOCAL",
    "PROVIDER_KIND_NONE",
    "PROVIDER_KIND_OLLAMA",
    "PROVIDER_KIND_OPENAI_COMPATIBLE",
    "PROVIDER_ROLES",
    "ROUTING_DISABLED",
    "ROUTING_LOCAL_ONLY",
    "ROUTING_POLICIES",
    "ROUTING_REMOTE_ALLOWED",
    "USABLE_MODEL_STATUSES",
    "LocalInferenceSettings",
    "LocalModelManifest",
    "ProcessingSettings",
    "ProviderRoleSettings",
    "WorkerSettings",
    "parse_settings",
]
