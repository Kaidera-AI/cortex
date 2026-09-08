"""OpenKai provider registry and host-owned provider-secret authority.

OpenKai owns the provider vocabulary and ``~/.openkai/.env`` owns provider
secrets.  This module is the narrow KOS port over those two upstream contracts:

* the vendored 21-provider credential projection is byte-pinned to the exact
  unpublished OpenKai UAT candidate and is an ordered ID subset of its immutable
  40-provider catalogue (the retained native 0.1.5 adapter remains executable only
  through its existing private-development gate and is not release evidence);
* status is masked and token-free;
* writes target only the first, canonical environment key from the registry;
* file mutation is owner checked, locked, revision guarded, fsynced and atomic.

No method returns, logs, or puts a secret in an exception message.  The one
internal ``resolved_environment`` method exists solely for the explicit legacy
Kaidera runner on the same host; it must never be exposed by an HTTP response.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .source_root import source_root


REGISTRY_SCHEMA = "openkai.providers.snapshot.v1"
REGISTRY_SOURCE_COMMIT = "f3660f3c19939d2a6ff3b95be9aab3f85fb8312a"
REGISTRY_SOURCE_PATH = "packages/cli/src/providers.ts"
REGISTRY_SOURCE_BLOB_SHA256 = "e5186b427385a77df6ae1a43b7930b71a9f6c3633211543f82621517fe1fe05c"
REGISTRY_SHA256 = "c71e04025710cbddf6fa1942b9752b9ac5c90837c82c7492c220ce945847d2de"
REGISTRY_BUNDLE_SHA256 = "a322a4153332c2842d98d26e0e3d164e7efe210dddaadd3216c69376fc1d6abf"
REGISTRY_CONTRACT_SHA256 = "12a8c017c7f74f4f2e2deac9f0fdf30cb96daefc17cfe920d8ec980c5d383726"
OPENKAI_RELEASE_TAG = "unpublished-uat-candidate"
OPENKAI_RELEASE_COMMIT = "f3660f3c19939d2a6ff3b95be9aab3f85fb8312a"
OPENKAI_DELIVERY_COMMIT = "f3660f3c19939d2a6ff3b95be9aab3f85fb8312a"
OPENKAI_DELIVERY_TREE = "8502a91c3cdf175817369cfa6496ff9cf3862593"
OPENKAI_AUTH_STORE_SOURCE_PATH = "packages/core/src/credentials.ts"
OPENKAI_AUTH_STORE_SOURCE_SHA256 = "083e709c7772ad17851233b14cd7344e69854afc0733faaf693f7be1f1e96f70"
OPENKAI_APPLIANCE_VERSION = "0.1.9-uat.0"
OPENKAI_APPLIANCE_RELEASE_TAG = "unpublished-uat-candidate"
OPENKAI_APPLIANCE_RELEASE_COMMIT = "f3660f3c19939d2a6ff3b95be9aab3f85fb8312a"
OPENKAI_APPLIANCE_PROVIDER_SOURCE_PATH = "packages/cli/src/providers.ts"
OPENKAI_APPLIANCE_PROVIDER_SOURCE_SHA256 = (
    "e5186b427385a77df6ae1a43b7930b71a9f6c3633211543f82621517fe1fe05c"
)
OPENKAI_APPLIANCE_PACKAGE_CATALOGUE_PATH = "dist/providers.js"
OPENKAI_APPLIANCE_PACKAGE_CATALOGUE_SHA256 = (
    "5245947e38a124ddf1740781bcf5873b29f910da6aa12fe1d320f76802066d7b"
)
OPENKAI_APPLIANCE_PROVIDER_COUNT = 40
OPENKAI_PROVIDER_PROJECTION_COUNT = 21
MAX_ENV_BYTES = 256 * 1024
MAX_AUTH_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 16 * 1024
PROJECTION_SCHEMA = "kos.openkai.provider-projection.v2"
PROJECTION_DIR_NAME = "kos-cortex-provider"
PROJECTION_ENV_NAME = "provider.env"
PROJECTION_MANIFEST_NAME = "manifest.json"
PROJECTION_PENDING_NAME = "pending.json"

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_ASSIGNMENT = re.compile(r"^(?P<indent>[ \t]*)(?P<export>export[ \t]+)?(?P<key>[A-Z_][A-Z0-9_]*)=(?P<value>.*)$")
_SAFE_UNQUOTED_VALUE = re.compile(r"^[^\s#'\"\x00-\x1f\x7f]+$")
_OPENKAI_MODEL_VALUE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._:/@+~-]{0,511}$")
OPENKAI_ROUTE_ENV_KEYS = frozenset({"OPENKAI_PROVIDER", "OPENKAI_MODEL"})

# A read-only, zero-completion network probe already exists for these providers.
# The host route maps the OpenKai ID to this legacy probe identifier without
# exposing the stored value to the container.
TEST_FIELD_BY_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "openrouter": "openrouter_api_key",
    "deepseek": "deepseek_api_key",
    "fireworks": "fireworks_api_key",
    "groq": "groq_api_key",
    "moonshotai": "moonshot_api_key",
    "nvidia": "nvidia_api_key",
    "together": "together_api_key",
    "xai": "xai_api_key",
}


class ProviderConfigError(RuntimeError):
    """A fail-closed provider registry/configuration error."""


class ProviderRegistryError(ProviderConfigError):
    """The vendored registry is absent, unpinned, or malformed."""


class ProviderConfigConflict(ProviderConfigError):
    """The caller's optimistic revision is stale."""


class ProviderConfigSecurityError(ProviderConfigError):
    """A filesystem ownership/type/symlink invariant failed."""


def default_registry_path() -> Path:
    override = (os.getenv("OPENKAI_PROVIDER_REGISTRY_FILE") or "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ProviderRegistryError("OpenKai provider registry path must be absolute")
        return path
    checkout = source_root()
    if checkout is not None:
        return checkout / "redistributable" / "config" / "openkai-providers.json"
    return Path(__file__).resolve().parent.parent / "config" / "openkai-providers.json"


def default_registry_pin_path() -> Path:
    return default_registry_path().with_name("openkai-provider-registry-pin.json")


def default_env_path() -> Path:
    # The pinned OpenKai CLI (>= 0.1.6) honours OPENKAI_HOME for this file
    # (packages/core/src/credentials.ts: openkaiHome() = OPENKAI_HOME ?? ~/.openkai),
    # KOS resolves the same authority the CLI writes. The earlier release-specific
    # exception is stale: in the appliance Path.home() is /home/kaidera, which does
    # not exist and is not on a volume, so writing there would both fail and fork
    # the credential plane.
    override = (os.getenv("OPENKAI_HOME") or "").strip()
    if override:
        return Path(override) / ".env"
    return Path.home() / ".openkai" / ".env"


def default_auth_path() -> Path:
    return Path.home() / ".openkai" / "auth.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_owned_regular_fd(fd: int, *, mode: int | None = None) -> os.stat_result:
    """Validate/chmod the already-open object, closing the lstat/open race."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise ProviderConfigSecurityError("provider configuration path is not a regular file")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise ProviderConfigSecurityError("provider configuration file has the wrong owner")
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        try:
            os.fchmod(fd, mode)
        except OSError as exc:
            raise ProviderConfigSecurityError("provider configuration permissions are unsafe") from exc
        st = os.fstat(fd)
        if stat.S_IMODE(st.st_mode) != mode:
            raise ProviderConfigSecurityError("provider configuration permissions are unsafe")
    return st


def _decode_value(raw: str, *, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ProviderConfigError(f"invalid provider env grammar at line {line_number}")
        value = value[1:-1]
    elif not _SAFE_UNQUOTED_VALUE.fullmatch(value):
        raise ProviderConfigError(f"invalid provider env grammar at line {line_number}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProviderConfigError(f"invalid provider env grammar at line {line_number}")
    return value


def parse_env(data: bytes) -> tuple[list[str], dict[str, str]]:
    """Parse the KOS-pinned OpenKai authority assignment grammar, without interpolation.

    Raw lines are returned so unrelated, valid assignments and comments survive a
    provider-key update.  Duplicate names are rejected as ambiguous rather than
    guessing which value the child will load.
    """
    if len(data) > MAX_ENV_BYTES:
        raise ProviderConfigError("provider configuration exceeds the size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderConfigError("provider configuration is not UTF-8") from exc
    lines = text.splitlines()
    values: dict[str, str] = {}
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ProviderConfigError(f"invalid provider env grammar at line {index}")
        key = match.group("key")
        if key in values:
            raise ProviderConfigError(f"duplicate provider env assignment at line {index}")
        values[key] = _decode_value(match.group("value"), line_number=index)
    return lines, values


class ProviderRegistry:
    """Validated view of the immutable OpenKai provider snapshot."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_registry_path()
        try:
            pin = json.loads(default_registry_pin_path().read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderRegistryError("OpenKai provider registry pin is unavailable") from exc
        expected_pin = {
            "schema_version": "kos.openkai.provider-registry-pin.v4",
            "openkai_version": "0.1.9-uat.0",
            "openkai_release_tag": OPENKAI_RELEASE_TAG,
            "openkai_release_commit": OPENKAI_RELEASE_COMMIT,
            "delivery_source_commit": OPENKAI_DELIVERY_COMMIT,
            "delivery_source_tree": OPENKAI_DELIVERY_TREE,
            "source_commit": REGISTRY_SOURCE_COMMIT,
            "source_path": REGISTRY_SOURCE_PATH,
            "source_blob_sha256": REGISTRY_SOURCE_BLOB_SHA256,
            "snapshot_sha256": REGISTRY_SHA256,
            "auth_store_source_path": OPENKAI_AUTH_STORE_SOURCE_PATH,
            "auth_store_source_sha256": OPENKAI_AUTH_STORE_SOURCE_SHA256,
            "appliance_projection_relationship": "ordered-exact-subset",
            "appliance_openkai_version": OPENKAI_APPLIANCE_VERSION,
            "appliance_openkai_release_tag": OPENKAI_APPLIANCE_RELEASE_TAG,
            "appliance_openkai_release_commit": OPENKAI_APPLIANCE_RELEASE_COMMIT,
            "appliance_provider_source_path": OPENKAI_APPLIANCE_PROVIDER_SOURCE_PATH,
            "appliance_provider_source_sha256": OPENKAI_APPLIANCE_PROVIDER_SOURCE_SHA256,
            "appliance_package_catalogue_path": OPENKAI_APPLIANCE_PACKAGE_CATALOGUE_PATH,
            "appliance_package_catalogue_sha256": OPENKAI_APPLIANCE_PACKAGE_CATALOGUE_SHA256,
            "appliance_runtime_provider_count": OPENKAI_APPLIANCE_PROVIDER_COUNT,
            "appliance_projection_provider_count": OPENKAI_PROVIDER_PROJECTION_COUNT,
            "integration_contract_sha256": REGISTRY_CONTRACT_SHA256,
            "bundle_sha256": REGISTRY_BUNDLE_SHA256,
        }
        if not isinstance(pin, dict) or any(pin.get(key) != value for key, value in expected_pin.items()):
            raise ProviderRegistryError("OpenKai provider registry pin mismatch")
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            raise ProviderRegistryError("OpenKai provider registry is unavailable") from exc
        if _sha256(data) != REGISTRY_SHA256:
            raise ProviderRegistryError("OpenKai provider registry hash mismatch")
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderRegistryError("OpenKai provider registry is invalid") from exc
        if not isinstance(payload, dict):
            raise ProviderRegistryError("OpenKai provider registry is invalid")
        if (
            payload.get("schema_version") != REGISTRY_SCHEMA
            or payload.get("source_commit") != REGISTRY_SOURCE_COMMIT
            or payload.get("source_path") != REGISTRY_SOURCE_PATH
        ):
            raise ProviderRegistryError("OpenKai provider registry provenance mismatch")
        projection = payload.get("appliance_projection")
        expected_projection = {
            "relationship": "ordered-exact-subset",
            "runtime_version": OPENKAI_APPLIANCE_VERSION,
            "runtime_release_tag": OPENKAI_APPLIANCE_RELEASE_TAG,
            "runtime_release_commit": OPENKAI_APPLIANCE_RELEASE_COMMIT,
            "runtime_source_path": OPENKAI_APPLIANCE_PROVIDER_SOURCE_PATH,
            "runtime_source_sha256": OPENKAI_APPLIANCE_PROVIDER_SOURCE_SHA256,
            "runtime_package_catalogue_path": OPENKAI_APPLIANCE_PACKAGE_CATALOGUE_PATH,
            "runtime_package_catalogue_sha256": OPENKAI_APPLIANCE_PACKAGE_CATALOGUE_SHA256,
            "runtime_provider_count": OPENKAI_APPLIANCE_PROVIDER_COUNT,
            "projection_provider_count": OPENKAI_PROVIDER_PROJECTION_COUNT,
        }
        if projection != expected_projection:
            raise ProviderRegistryError("OpenKai appliance provider projection mismatch")
        rows = payload.get("providers")
        if not isinstance(rows, list) or len(rows) != OPENKAI_PROVIDER_PROJECTION_COUNT:
            raise ProviderRegistryError("OpenKai provider registry provider count mismatch")
        providers: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_env: set[str] = set()
        for raw in rows:
            if not isinstance(raw, dict):
                raise ProviderRegistryError("OpenKai provider registry row is invalid")
            provider_id = str(raw.get("id") or "")
            label = str(raw.get("label") or "")
            env_keys_raw = raw.get("env_keys")
            if (
                not provider_id
                or provider_id in seen_ids
                or not label
                or not isinstance(env_keys_raw, list)
                or not isinstance(raw.get("oauth"), bool)
            ):
                raise ProviderRegistryError("OpenKai provider registry row is invalid")
            env_keys = [str(item) for item in env_keys_raw]
            if any(not _ENV_NAME.fullmatch(item) for item in env_keys):
                raise ProviderRegistryError("OpenKai provider registry env key is invalid")
            if len(set(env_keys)) != len(env_keys):
                raise ProviderRegistryError("OpenKai provider registry env key is duplicated")
            # An accepted alias may not point at two providers: that would make a
            # masked presence row and a canonical write target ambiguous.
            if seen_env.intersection(env_keys):
                raise ProviderRegistryError("OpenKai provider registry env key is ambiguous")
            seen_ids.add(provider_id)
            seen_env.update(env_keys)
            providers.append(
                {
                    "id": provider_id,
                    "label": label,
                    "env_keys": tuple(env_keys),
                    "oauth": bool(raw["oauth"]),
                }
            )
        default_provider = str(payload.get("default_provider") or "")
        if default_provider not in seen_ids:
            raise ProviderRegistryError("OpenKai default provider is invalid")
        self.providers = tuple(providers)
        self.default_provider = default_provider
        self.by_id = {row["id"]: row for row in self.providers}
        self.env_keys = frozenset(seen_env)


class ProviderConfigService:
    """Hardened host-side port over ``~/.openkai/.env``."""

    def __init__(
        self,
        *,
        registry_path: Path | None = None,
        env_path: Path | None = None,
        auth_path: Path | None = None,
        projection_dir: Path | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.registry = ProviderRegistry(registry_path)
        self.env_path = Path(env_path) if env_path is not None else default_env_path()
        self.auth_path = (
            Path(auth_path)
            if auth_path is not None
            else self.env_path.with_name("auth.json")
        )
        self.lock_path = self.env_path.with_name(f"{self.env_path.name}.lock")
        self.projection_dir = (
            Path(projection_dir)
            if projection_dir is not None
            else self.env_path.parent / PROJECTION_DIR_NAME
        )
        if self.projection_dir.parent != self.env_path.parent:
            raise ProviderConfigSecurityError(
                "provider projection must be inside the OpenKai configuration directory"
            )
        self.projection_env_path = self.projection_dir / PROJECTION_ENV_NAME
        self.projection_manifest_path = (
            self.projection_dir / PROJECTION_MANIFEST_NAME
        )
        self.projection_pending_path = self.projection_dir / PROJECTION_PENDING_NAME
        self._fault_hook = fault_hook

    def _fault(self, phase: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(phase)

    @staticmethod
    def _validate_private_read_parent(parent: Path) -> bool:
        """Reject symlinked ancestry and require the exact leaf owner/mode.

        This is deliberately validation-only: KOS never chmods or otherwise
        mutates OpenKai's OAuth credential store while inspecting metadata.
        """
        absolute = parent.absolute()
        candidate = Path(absolute.anchor)
        try:
            for part in absolute.parts[1:]:
                candidate = candidate / part
                try:
                    metadata = candidate.lstat()
                except FileNotFoundError:
                    return False
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise ProviderConfigSecurityError(
                        "OpenKai credential-store ancestor is unsafe"
                    )
                if hasattr(os, "geteuid") and metadata.st_uid not in {0, os.geteuid()}:
                    raise ProviderConfigSecurityError(
                        "OpenKai credential-store ancestor has the wrong owner"
                    )
            metadata = parent.lstat()
            if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
                raise ProviderConfigSecurityError(
                    "OpenKai credential-store directory has the wrong owner"
                )
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ProviderConfigSecurityError(
                    "OpenKai credential-store directory permissions are unsafe"
                )
            return True
        except ProviderConfigSecurityError:
            raise
        except OSError as exc:
            raise ProviderConfigSecurityError(
                "OpenKai credential-store directory is unavailable"
            ) from exc

    def _ensure_parent(self) -> None:
        parent = self.env_path.parent
        try:
            # Walk every already-existing path component with lstat before any
            # mkdir/open. This catches a symlink hidden above an otherwise ordinary
            # terminal parent; root-owned system anchors and the effective user are
            # the only accepted owners.
            absolute = parent.absolute()
            candidate = Path(absolute.anchor)
            for part in absolute.parts[1:]:
                candidate = candidate / part
                try:
                    component = candidate.lstat()
                except FileNotFoundError:
                    break
                if stat.S_ISLNK(component.st_mode) or not stat.S_ISDIR(component.st_mode):
                    raise ProviderConfigSecurityError(
                        "OpenKai configuration ancestor is unsafe"
                    )
                if hasattr(os, "geteuid") and component.st_uid not in {0, os.geteuid()}:
                    raise ProviderConfigSecurityError(
                        "OpenKai configuration ancestor has the wrong owner"
                    )
            # Validate the closest existing ancestor before creating anything.  A
            # deterministic path below a foreign-owned/world-writable ancestor is
            # not a safe secret root, and an ancestor symlink must never be followed.
            current = parent
            closest_existing: Path | None = None
            while True:
                try:
                    st = current.lstat()
                except FileNotFoundError:
                    if current == current.parent:
                        break
                    current = current.parent
                    continue
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                    raise ProviderConfigSecurityError("OpenKai configuration ancestor is unsafe")
                closest_existing = current
                if current != Path(current.anchor):
                    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                        raise ProviderConfigSecurityError(
                            "OpenKai configuration ancestor has the wrong owner"
                        )
                break
            if closest_existing is None:
                raise ProviderConfigSecurityError("OpenKai configuration ancestor is unavailable")

            # Any already-existing component between that anchor and the target is
            # also checked explicitly.  pathlib mkdir must not traverse a symlink
            # hidden in an intermediate component.
            relative_parts = parent.relative_to(closest_existing).parts
            candidate = closest_existing
            for part in relative_parts:
                candidate = candidate / part
                try:
                    st = candidate.lstat()
                except FileNotFoundError:
                    break
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                    raise ProviderConfigSecurityError("OpenKai configuration ancestor is unsafe")
                if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                    raise ProviderConfigSecurityError(
                        "OpenKai configuration ancestor has the wrong owner"
                    )
            if parent.exists() or parent.is_symlink():
                st = parent.lstat()
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                    raise ProviderConfigSecurityError("OpenKai configuration directory is unsafe")
                if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                    raise ProviderConfigSecurityError("OpenKai configuration directory has the wrong owner")
                if stat.S_IMODE(st.st_mode) != 0o700:
                    parent.chmod(0o700)
            else:
                # Two first writers may race here before they converge on the
                # flock file.  exist_ok handles only that benign mkdir race; the
                # lstat/type/owner/mode checks immediately below still reject a
                # substituted symlink or foreign directory.
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            st = parent.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise ProviderConfigSecurityError("OpenKai configuration directory is unsafe")
            if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                raise ProviderConfigSecurityError("OpenKai configuration directory has the wrong owner")
            if stat.S_IMODE(st.st_mode) != 0o700:
                raise ProviderConfigSecurityError("OpenKai configuration directory permissions are unsafe")
        except ProviderConfigSecurityError:
            raise
        except OSError as exc:
            raise ProviderConfigSecurityError("OpenKai configuration directory is unavailable") from exc

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._ensure_parent()
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ProviderConfigSecurityError("provider configuration lock is unavailable") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or (
                hasattr(os, "geteuid") and st.st_uid != os.geteuid()
            ):
                raise ProviderConfigSecurityError("provider configuration lock is unsafe")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_bytes(self) -> bytes:
        if self.env_path.parent.exists() or self.env_path.parent.is_symlink():
            self._ensure_parent()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.env_path, flags)
        except FileNotFoundError:
            return b""
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ProviderConfigSecurityError(
                    "provider configuration path is not a regular file"
                ) from exc
            raise ProviderConfigSecurityError("provider configuration file is unavailable") from exc
        try:
            _validate_owned_regular_fd(fd, mode=0o600)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, MAX_ENV_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_ENV_BYTES:
                    raise ProviderConfigError("provider configuration exceeds the size limit")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _read_auth_bytes(self) -> bytes | None:
        """Read OpenKai-owned OAuth storage without ever modifying its metadata."""
        if not self._validate_private_read_parent(self.auth_path.parent):
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.auth_path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ProviderConfigSecurityError(
                    "OpenKai credential-store path is unsafe"
                ) from exc
            raise ProviderConfigSecurityError(
                "OpenKai credential store is unavailable"
            ) from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ProviderConfigSecurityError(
                    "OpenKai credential-store path is unsafe"
                )
            if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
                raise ProviderConfigSecurityError(
                    "OpenKai credential store has the wrong owner"
                )
            # Metadata inspection is strictly read-only. Reject a loose mode; do
            # not chmod a file whose writer/owner is OpenKai.
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ProviderConfigSecurityError(
                    "OpenKai credential-store permissions are unsafe"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, MAX_AUTH_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_AUTH_BYTES:
                    raise ProviderConfigError(
                        "OpenKai credential-store metadata exceeds the size limit"
                    )
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _credential_states_from_bytes(
        self, data: bytes | None
    ) -> dict[str, str]:
        """Sanitize OpenKai auth bytes into eligibility states only.

        OAuth presence is deliberately a HOLD, not execution approval: tagged
        v0.1.005 serializes only in-process, while KOS can launch concurrent
        one-shot OpenKai processes. Stored ``api_key`` entries are an authority
        conflict because ``.env`` remains KOS's sole API-key store.
        """
        if data is None:
            return {}
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return {
                row["id"]: "unverified"
                for row in self.registry.providers
            }
        if not isinstance(payload, dict):
            return {
                row["id"]: "unverified"
                for row in self.registry.providers
            }
        states: dict[str, str] = {}
        for provider_id, credential in payload.items():
            descriptor = self.registry.by_id.get(str(provider_id))
            if descriptor is None:
                continue
            if not isinstance(credential, dict):
                states[descriptor["id"]] = "unverified"
                continue
            credential_type = credential.get("type")
            if credential_type == "api_key":
                states[descriptor["id"]] = "api_key_authority_conflict"
                continue
            expires = credential.get("expires")
            oauth_shape = (
                credential_type == "oauth"
                and descriptor["oauth"]
                and isinstance(credential.get("access"), str)
                and bool(credential.get("access"))
                and isinstance(credential.get("refresh"), str)
                and bool(credential.get("refresh"))
                and isinstance(expires, (int, float))
                and not isinstance(expires, bool)
                and math.isfinite(float(expires))
            )
            states[descriptor["id"]] = (
                "oauth_present_held" if oauth_shape else "unverified"
            )
        return states

    def _credential_states(self) -> dict[str, str]:
        """Read only a token-free eligibility view of OpenKai-owned auth.json."""
        return self._credential_states_from_bytes(self._read_auth_bytes())

    def _credential_state_vector(
        self, credential_states: dict[str, str]
    ) -> tuple[tuple[str, str], ...]:
        """Stable, token-free vector used to bind projection eligibility."""
        return tuple(
            (
                descriptor["id"],
                credential_states.get(descriptor["id"], "absent"),
            )
            for descriptor in self.registry.providers
        )

    def projection_revision_for(
        self,
        authority_data: bytes,
        credential_states: dict[str, str] | None = None,
    ) -> str:
        """Composite revision over env bytes and sanitized auth eligibility.

        Raw auth values never enter the digest. Token refreshes that preserve the
        same held/absent/conflict state do not churn the projection; any state
        transition that changes whether an API key may be projected does.
        """
        states = (
            self._credential_states()
            if credential_states is None
            else credential_states
        )
        state_bytes = json.dumps(
            self._credential_state_vector(states),
            separators=(",", ":"),
        ).encode("utf-8")
        payload = (
            b"kos.openkai.provider-projection.revision.v2\0"
            + bytes.fromhex(self.revision_for(authority_data))
            + b"\0"
            + state_bytes
        )
        return _sha256(payload)

    def auth_credential_state(self, provider_id: str) -> str:
        """Token-free runtime/status seam; never reads values into a response."""
        descriptor = self._descriptor(provider_id)
        state = self._credential_states().get(descriptor["id"], "absent")
        return state

    def oauth_env_alias_present(self, provider_id: str) -> bool:
        """Whether only a noncanonical OAuth/auth env alias is configured.

        Tagged UAT accepts API keys only. The exact alias remains readable by
        OpenKai compatibility code, but KOS neither relabels nor executes it.
        """
        descriptor = self._descriptor(provider_id)
        env_keys = descriptor["env_keys"]
        if not descriptor["oauth"] or len(env_keys) < 2:
            return False
        _data, _lines, values = self._read_parsed()
        return not values.get(env_keys[0]) and any(
            values.get(key) for key in env_keys[1:]
        )

    def raw_backup(self) -> bytes:
        """Internal migration seam.  Returned bytes are secret and must be protected."""
        return self._read_bytes()

    def file_topology(self) -> tuple[bool, int | None]:
        """Read existence/mode without creating a directory or normalizing mode."""
        try:
            metadata = self.env_path.lstat()
        except FileNotFoundError:
            return False, None
        except OSError as exc:
            raise ProviderConfigSecurityError(
                "provider configuration file is unavailable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ProviderConfigSecurityError(
                "provider configuration path is not a regular file"
            )
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise ProviderConfigSecurityError(
                "provider configuration file has the wrong owner"
            )
        return True, stat.S_IMODE(metadata.st_mode)

    def snapshot(self) -> tuple[bytes, bool, int | None]:
        """Return exact bytes/existence/mode for a protected migration before-image.

        This is the only read that deliberately does not normalize the file mode:
        rollback must distinguish absent from empty and restore the exact pre-cutover
        topology.  The tuple is secret-bearing and must never cross an HTTP boundary.
        """
        with self._lock():
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(self.env_path, flags)
            except FileNotFoundError:
                return b"", False, None
            except OSError as exc:
                raise ProviderConfigSecurityError(
                    "provider configuration file is unavailable"
                ) from exc
            try:
                metadata = _validate_owned_regular_fd(fd)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, min(64 * 1024, MAX_ENV_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_ENV_BYTES:
                        raise ProviderConfigError(
                            "provider configuration exceeds the size limit"
                        )
                data = b"".join(chunks)
                parse_env(data)
                return data, True, stat.S_IMODE(metadata.st_mode)
            finally:
                os.close(fd)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _ensure_projection_dir(self) -> None:
        """Create/validate only OpenKai's product-owned Cortex projection leaf."""
        self._ensure_parent()
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        parent_fd = os.open(self.env_path.parent, flags)
        try:
            try:
                os.mkdir(self.projection_dir.name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            projection_fd = os.open(self.projection_dir.name, flags, dir_fd=parent_fd)
            try:
                metadata = os.fstat(projection_fd)
                if not stat.S_ISDIR(metadata.st_mode) or (
                    hasattr(os, "geteuid") and metadata.st_uid != os.geteuid()
                ):
                    raise ProviderConfigSecurityError(
                        "provider projection directory is unsafe"
                    )
                os.fchmod(projection_fd, 0o700)
                if stat.S_IMODE(os.fstat(projection_fd).st_mode) != 0o700:
                    raise ProviderConfigSecurityError(
                        "provider projection directory permissions are unsafe"
                    )
                os.fsync(projection_fd)
            finally:
                os.close(projection_fd)
            os.fsync(parent_fd)
        except ProviderConfigError:
            raise
        except OSError as exc:
            raise ProviderConfigSecurityError(
                "provider projection directory is unavailable"
            ) from exc
        finally:
            os.close(parent_fd)

    def _atomic_private_write(self, path: Path, data: bytes) -> None:
        """Owner-private, same-directory, durable atomic replacement."""
        fd = -1
        temp_name = ""
        try:
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
            temp_name = ""
            verify_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                verify_flags |= os.O_NOFOLLOW
            verify_fd = os.open(path, verify_flags)
            try:
                _validate_owned_regular_fd(verify_fd, mode=0o600)
                os.fsync(verify_fd)
            finally:
                os.close(verify_fd)
            self._fsync_directory(path.parent)
        except ProviderConfigError:
            raise
        except OSError as exc:
            raise ProviderConfigSecurityError("private configuration update failed") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def _projection_data(
        self,
        authority_data: bytes,
        *,
        credential_states: dict[str, str],
    ) -> bytes:
        """Canonical API-key subset; aliases/OAuth/unrelated values never cross."""
        _lines, values = parse_env(authority_data)
        projected: list[str] = []
        for descriptor in self.registry.providers:
            env_keys = descriptor["env_keys"]
            if not env_keys:
                continue
            present = [key for key in env_keys if values.get(key, "")]
            if len(present) > 1:
                # Keep status/remove available so an operator can repair the
                # conflict, but never project an ambiguous credential.
                continue
            canonical = env_keys[0]
            value = values.get(canonical, "")
            if not value or credential_states.get(descriptor["id"], "absent") != "absent":
                continue
            if not _SAFE_UNQUOTED_VALUE.fullmatch(value):
                raise ProviderConfigError(
                    "provider credential cannot be represented in the Cortex projection"
                )
            projected.append(f"{canonical}={value}")
        return (("\n".join(projected) + "\n") if projected else "").encode("utf-8")

    def _projection_manifest(
        self,
        authority_data: bytes,
        *,
        authority_existed: bool,
        projection_data: bytes,
        credential_states: dict[str, str],
    ) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": PROJECTION_SCHEMA,
                    "registry_sha256": REGISTRY_SHA256,
                    "authority_existed": authority_existed,
                    "authority_revision": self.projection_revision_for(
                        authority_data, credential_states
                    ),
                    "projection_sha256": _sha256(projection_data),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _projection_begin(
        self,
        authority_data: bytes,
        *,
        authority_existed: bool,
        credential_states: dict[str, str],
    ) -> None:
        self._ensure_projection_dir()
        pending = (
            json.dumps(
                {
                    "schema_version": PROJECTION_SCHEMA,
                    "authority_existed": authority_existed,
                    "target_authority_revision": self.projection_revision_for(
                        authority_data, credential_states
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self._atomic_private_write(self.projection_pending_path, pending)
        self._fault("projection_pending")

    def _projection_remove_pending(self) -> None:
        try:
            metadata = self.projection_pending_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or (
            hasattr(os, "geteuid") and metadata.st_uid != os.geteuid()
        ):
            raise ProviderConfigSecurityError("provider projection pending marker is unsafe")
        os.unlink(self.projection_pending_path)
        self._fsync_directory(self.projection_dir)

    def _projection_prune_temps(self) -> None:
        prefixes = tuple(
            f".{name}."
            for name in (
                PROJECTION_ENV_NAME,
                PROJECTION_MANIFEST_NAME,
                PROJECTION_PENDING_NAME,
            )
        )
        for entry in os.scandir(self.projection_dir):
            if not entry.name.startswith(prefixes):
                continue
            metadata = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or (
                hasattr(os, "geteuid") and metadata.st_uid != os.geteuid()
            ):
                raise ProviderConfigSecurityError("provider projection temporary file is unsafe")
            os.unlink(entry.path)
        self._fsync_directory(self.projection_dir)

    def _projection_finish(
        self,
        authority_data: bytes,
        *,
        authority_existed: bool,
        credential_states: dict[str, str],
    ) -> None:
        projection_data = self._projection_data(
            authority_data, credential_states=credential_states
        )
        manifest = self._projection_manifest(
            authority_data,
            authority_existed=authority_existed,
            projection_data=projection_data,
            credential_states=credential_states,
        )
        self._atomic_private_write(self.projection_env_path, projection_data)
        self._fault("projection_env_written")
        self._atomic_private_write(self.projection_manifest_path, manifest)
        self._fault("projection_manifest_written")
        self._projection_remove_pending()
        self._projection_prune_temps()
        self._fault("projection_committed")

    def _read_private_projection_file(self, path: Path, *, limit: int) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            _validate_owned_regular_fd(fd, mode=0o600)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise ProviderConfigError(
                        "provider projection exceeds the size limit"
                    )
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _projection_matches(
        self,
        authority_data: bytes,
        *,
        authority_existed: bool,
        credential_states: dict[str, str],
    ) -> bool:
        self._ensure_projection_dir()
        if self.projection_pending_path.exists() or self.projection_pending_path.is_symlink():
            return False
        try:
            projection_data = self._read_private_projection_file(
                self.projection_env_path, limit=MAX_ENV_BYTES
            )
            manifest_data = self._read_private_projection_file(
                self.projection_manifest_path, limit=16 * 1024
            )
            manifest = json.loads(manifest_data)
        except FileNotFoundError:
            return False
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        expected_projection = self._projection_data(
            authority_data, credential_states=credential_states
        )
        if not hmac.compare_digest(projection_data, expected_projection):
            return False
        expected = json.loads(
            self._projection_manifest(
                authority_data,
                authority_existed=authority_existed,
                projection_data=expected_projection,
                credential_states=credential_states,
            )
        )
        if manifest != expected:
            return False
        # Stable state contains exactly the two mounted files. Unknown material
        # is held rather than silently exposed to the Cortex container.
        names = {entry.name for entry in os.scandir(self.projection_dir)}
        return names == {PROJECTION_ENV_NAME, PROJECTION_MANIFEST_NAME}

    def _reconcile_projection_locked(
        self, authority_data: bytes, *, authority_existed: bool
    ) -> str:
        """Converge against two equal sanitized auth snapshots.

        OpenKai owns auth.json and may atomically replace it without taking the
        provider-env lock. Two equal state-vector reads ensure the committed
        projection represents a stable eligibility observation; rapid churn
        fails closed after a bounded number of attempts.
        """
        for _attempt in range(3):
            credential_states = self._credential_states()
            if not self._projection_matches(
                authority_data,
                authority_existed=authority_existed,
                credential_states=credential_states,
            ):
                self._projection_begin(
                    authority_data,
                    authority_existed=authority_existed,
                    credential_states=credential_states,
                )
                self._projection_finish(
                    authority_data,
                    authority_existed=authority_existed,
                    credential_states=credential_states,
                )
            after = self._credential_states()
            if not hmac.compare_digest(
                json.dumps(
                    self._credential_state_vector(credential_states),
                    separators=(",", ":"),
                ),
                json.dumps(
                    self._credential_state_vector(after),
                    separators=(",", ":"),
                ),
            ):
                continue
            if self._projection_matches(
                authority_data,
                authority_existed=authority_existed,
                credential_states=after,
            ):
                return self.projection_revision_for(authority_data, after)
        raise ProviderConfigSecurityError("provider projection did not converge")

    def reconcile_projection(self) -> dict[str, Any]:
        """Resume or refresh the derived Cortex cache from the sole authority."""
        with self._lock():
            authority_existed, _mode = self.file_topology()
            authority_data = self._read_bytes()
            projection_revision = self._reconcile_projection_locked(
                authority_data, authority_existed=authority_existed
            )
        return {
            "schema_version": PROJECTION_SCHEMA,
            "authority_revision": projection_revision,
        }

    def _atomic_write(self, data: bytes) -> None:
        if len(data) > MAX_ENV_BYTES:
            raise ProviderConfigError("provider configuration exceeds the size limit")
        parse_env(data)
        self._ensure_parent()
        credential_states = self._credential_states()
        self._projection_begin(
            data,
            authority_existed=True,
            credential_states=credential_states,
        )
        self._atomic_private_write(self.env_path, data)
        self._fault("authority_written")
        self._projection_finish(
            data,
            authority_existed=True,
            credential_states=credential_states,
        )
        self._reconcile_projection_locked(data, authority_existed=True)

    @staticmethod
    def revision_for(data: bytes) -> str:
        return _sha256(data)

    def _read_parsed(self) -> tuple[bytes, list[str], dict[str, str]]:
        data = self._read_bytes()
        lines, values = parse_env(data)
        return data, lines, values

    def status(self) -> dict[str, Any]:
        self.reconcile_projection()
        data, _lines, values = self._read_parsed()
        credential_states = self._credential_states()
        rows: list[dict[str, Any]] = []
        for descriptor in self.registry.providers:
            env_keys = descriptor["env_keys"]
            source_key = next((key for key in env_keys if values.get(key, "") != ""), "")
            present_keys = [key for key in env_keys if values.get(key, "")]
            alias_conflict = len(present_keys) > 1
            oauth_only = bool(descriptor["oauth"] and not env_keys)
            oauth_env_present = bool(
                descriptor["oauth"]
                and source_key
                and source_key != (env_keys[0] if env_keys else "")
            )
            credential_state = credential_states.get(descriptor["id"], "absent")
            if alias_conflict:
                credential_state = "env_alias_conflict"
            elif credential_state == "absent" and oauth_env_present:
                credential_state = "oauth_env_alias_held"
            oauth_present = credential_state == "oauth_present_held"
            authority_conflict = credential_state == "api_key_authority_conflict"
            if not descriptor["oauth"] and credential_state == "absent":
                oauth_state = "not_applicable"
            elif oauth_present:
                oauth_state = "present_held"
            elif credential_state == "unverified":
                oauth_state = "unverified"
            elif authority_conflict:
                oauth_state = "api_key_authority_conflict"
            elif oauth_env_present:
                oauth_state = "env_alias_held"
            else:
                oauth_state = "not_configured"
            api_key_configured = bool(
                source_key and not oauth_env_present and not alias_conflict
            )
            rows.append(
                {
                    "id": descriptor["id"],
                    "name": descriptor["id"],
                    "label": descriptor["label"],
                    "canonical_env_key": env_keys[0] if env_keys else None,
                    "accepted_env_keys": list(env_keys),
                    "configured": api_key_configured,
                    "key_is_set": api_key_configured,
                    "source_env_key": source_key or None,
                    "key_source": "openkai_env" if source_key else "",
                    "oauth": bool(descriptor["oauth"]),
                    "oauth_present": oauth_present,
                    "oauth_env_present": oauth_env_present,
                    "oauth_state": oauth_state,
                    "oauth_only": oauth_only,
                    "write_supported": bool(env_keys),
                    "testable": bool(
                        api_key_configured
                        and source_key == env_keys[0]
                        and credential_state == "absent"
                        and descriptor["id"] in TEST_FIELD_BY_PROVIDER
                    ),
                    "execution_ready": bool(
                        api_key_configured and credential_state == "absent"
                    ),
                    "credential_state": credential_state,
                    "authority_conflict": authority_conflict,
                    "alias_conflict": alias_conflict,
                    "provider_ref": descriptor["id"],
                    "is_custom": False,
                }
            )
        return {
            "schema_version": REGISTRY_SCHEMA,
            "openkai_release_tag": OPENKAI_RELEASE_TAG,
            "openkai_release_commit": OPENKAI_RELEASE_COMMIT,
            "source_commit": REGISTRY_SOURCE_COMMIT,
            "source_path": REGISTRY_SOURCE_PATH,
            "registry_sha256": REGISTRY_SHA256,
            "bundle_sha256": REGISTRY_BUNDLE_SHA256,
            "oauth_uat_policy": "metadata_only_api_key_uat_cross_process_lock_hold",
            "appliance_projection": {
                "relationship": "ordered-exact-subset",
                "runtime_version": OPENKAI_APPLIANCE_VERSION,
                "runtime_release_tag": OPENKAI_APPLIANCE_RELEASE_TAG,
                "runtime_release_commit": OPENKAI_APPLIANCE_RELEASE_COMMIT,
                "runtime_provider_count": OPENKAI_APPLIANCE_PROVIDER_COUNT,
                "projection_provider_count": OPENKAI_PROVIDER_PROJECTION_COUNT,
            },
            "default_provider": self.registry.default_provider,
            "revision": self.revision_for(data),
            "projection_revision": self.projection_revision_for(
                data, credential_states
            ),
            "providers": rows,
        }

    def _descriptor(self, provider_id: str) -> dict[str, Any]:
        row = self.registry.by_id.get((provider_id or "").strip())
        if row is None:
            raise ProviderConfigError("provider is not in the pinned OpenKai registry")
        return row

    @staticmethod
    def _validate_secret(secret: str) -> str:
        if not isinstance(secret, str):
            raise ProviderConfigError("provider secret must be a string")
        raw = secret.encode("utf-8")
        if not raw or len(raw) > MAX_SECRET_BYTES or not _SAFE_UNQUOTED_VALUE.fullmatch(secret):
            raise ProviderConfigError("provider secret does not satisfy the OpenKai env grammar")
        return secret

    @staticmethod
    def _replace_assignments(lines: list[str], keys: tuple[str, ...], key: str, value: str | None) -> bytes:
        out: list[str] = []
        for line in lines:
            match = _ASSIGNMENT.fullmatch(line)
            if match is not None and match.group("key") in keys:
                continue
            out.append(line)
        if value is not None:
            out.append(f"{key}={value}")
        return (("\n".join(out) + "\n") if out else "").encode("utf-8")

    def patch(self, provider_id: str, secret: str, *, expected_revision: str) -> dict[str, Any]:
        descriptor = self._descriptor(provider_id)
        env_keys = descriptor["env_keys"]
        if not env_keys:
            raise ProviderConfigError("OAuth-only credentials are owned by OpenKai, not KOS")
        value = self._validate_secret(secret)
        if not isinstance(expected_revision, str) or len(expected_revision) != 64:
            raise ProviderConfigConflict("provider configuration revision is required")
        with self._lock():
            data, lines, _values = self._read_parsed()
            if not secrets_equal_revision(expected_revision, self.revision_for(data)):
                raise ProviderConfigConflict("provider configuration changed; refresh before saving")
            self._atomic_write(
                self._replace_assignments(lines, env_keys, env_keys[0], value)
            )
        return self.status()

    def remove(self, provider_id: str, *, expected_revision: str) -> dict[str, Any]:
        descriptor = self._descriptor(provider_id)
        env_keys = descriptor["env_keys"]
        if not env_keys:
            raise ProviderConfigError("OAuth-only credentials are owned by OpenKai, not KOS")
        if not isinstance(expected_revision, str) or len(expected_revision) != 64:
            raise ProviderConfigConflict("provider configuration revision is required")
        with self._lock():
            data, lines, _values = self._read_parsed()
            if not secrets_equal_revision(expected_revision, self.revision_for(data)):
                raise ProviderConfigConflict("provider configuration changed; refresh before removing")
            self._atomic_write(
                self._replace_assignments(lines, env_keys, env_keys[0], None)
            )
        return self.status()

    def restore_exact(self, data: bytes, *, expected_revision: str) -> dict[str, Any]:
        """Migration rollback seam: restore previously backed-up bytes exactly."""
        return self.restore_snapshot(
            data,
            existed=True,
            mode=0o600,
            expected_revision=expected_revision,
        )

    def restore_snapshot(
        self,
        data: bytes,
        *,
        existed: bool,
        mode: int | None,
        expected_revision: str,
    ) -> dict[str, Any]:
        """Restore exact migration topology, including a previously absent file."""
        parse_env(data)
        if existed and (not isinstance(mode, int) or mode < 0 or mode > 0o7777):
            raise ProviderConfigSecurityError("provider backup mode is invalid")
        with self._lock():
            current = self._read_bytes()
            if not secrets_equal_revision(expected_revision, self.revision_for(current)):
                raise ProviderConfigConflict("provider configuration changed; rollback held")
            if existed:
                self._atomic_write(data)
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(self.env_path, flags)
                try:
                    _validate_owned_regular_fd(fd)
                    os.fchmod(fd, int(mode))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            else:
                credential_states = self._credential_states()
                self._projection_begin(
                    b"",
                    authority_existed=False,
                    credential_states=credential_states,
                )
                try:
                    os.unlink(self.env_path)
                except FileNotFoundError:
                    pass
                dir_flags = os.O_RDONLY
                if hasattr(os, "O_DIRECTORY"):
                    dir_flags |= os.O_DIRECTORY
                if hasattr(os, "O_NOFOLLOW"):
                    dir_flags |= os.O_NOFOLLOW
                dir_fd = os.open(self.env_path.parent, dir_flags)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                self._fault("authority_removed")
                self._projection_finish(
                    b"",
                    authority_existed=False,
                    credential_states=credential_states,
                )
                self._reconcile_projection_locked(
                    b"", authority_existed=False
                )
        # Do not call status(): it intentionally normalizes an active authority to
        # 0600, while rollback must preserve the exact historical mode/topology.
        return {
            "schema_version": REGISTRY_SCHEMA,
            "revision": self.revision_for(data if existed else b""),
        }

    def _resolved_environment_from_values(
        self, values: dict[str, str]
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for descriptor in self.registry.providers:
            env_keys = descriptor["env_keys"]
            if not env_keys:
                continue
            present = [(key, values[key]) for key in env_keys if values.get(key, "") != ""]
            if len(present) > 1:
                raise ProviderConfigError("provider env aliases contain conflicting credentials")
            if present:
                # Preserve the exact source name.  In particular, an Anthropic
                # OAuth/auth token is not relabelled as ANTHROPIC_API_KEY.
                resolved[present[0][0]] = present[0][1]
        return resolved

    def resolved_environment(self) -> dict[str, str]:
        """Internal host-only credential view for OpenKai-compatible fallback.

        Accepted aliases retain their exact source name in the returned child
        environment.  Callers must never serialize, log, or persist this mapping.
        """
        _data, _lines, values = self._read_parsed()
        return self._resolved_environment_from_values(values)

    def secret_for_provider(self, provider_id: str) -> str:
        """Any accepted OpenKai credential for conflict/migration comparison."""
        descriptor = self._descriptor(provider_id)
        env_keys = descriptor["env_keys"]
        if not env_keys:
            return ""
        resolved = self.resolved_environment()
        return next((resolved[key] for key in env_keys if resolved.get(key)), "")

    def canonical_secret_for_provider(self, provider_id: str) -> str:
        """Exact canonical API-key value for KOS-owned network probes only.

        Compatibility names may represent a different credential type. OpenKai
        can consume them under their original name, but KOS must never relabel an
        alias into the canonical probe header.
        """
        descriptor = self._descriptor(provider_id)
        env_keys = descriptor["env_keys"]
        if not env_keys:
            return ""
        for _attempt in range(3):
            before = self._credential_states_from_bytes(self._read_auth_bytes())
            resolved = self.resolved_environment()
            after = self._credential_states_from_bytes(self._read_auth_bytes())
            if self._credential_state_vector(before) != self._credential_state_vector(after):
                continue
            if after.get(descriptor["id"], "absent") != "absent":
                return ""
            return resolved.get(env_keys[0], "")
        raise ProviderConfigError("OpenKai credential-store metadata changed during read")

    def secret_for_env(self, env_key: str) -> str:
        if env_key not in self.registry.env_keys:
            return ""
        # An alias may have different credential semantics (Anthropic OAuth vs
        # API key), so an exact-name lookup never retypes another accepted key.
        return self.resolved_environment().get(env_key, "")


def secrets_equal_revision(left: str, right: str) -> bool:
    """Constant-time comparison for public-but-concurrency-sensitive revisions."""
    import hmac

    return hmac.compare_digest(left, right)


def legacy_provider_environment() -> dict[str, str]:
    """Exact shared-secret source for the explicit legacy Kaidera fallback."""
    return ProviderConfigService().resolved_environment()


def provider_ids() -> tuple[str, ...]:
    """Pinned OpenKai provider IDs; safe for runtime routing/allowlisting."""
    return tuple(row["id"] for row in ProviderConfigService().registry.providers)


def provider_env_keys() -> frozenset[str]:
    """Every provider/route env name an OpenKai child must scrub first."""
    return ProviderConfigService().registry.env_keys | OPENKAI_ROUTE_ENV_KEYS


def default_provider_id() -> str:
    """Return only the configured/default provider ID, never a credential.

    Execution uses this metadata seam to make the edition/licence decision before
    asking the authority for the selected secret. A later snapshot must repeat the
    same route, closing a configuration-change race fail-closed.
    """
    service = ProviderConfigService()
    _data, _lines, values = service._read_parsed()
    provider_id = values.get("OPENKAI_PROVIDER", service.registry.default_provider)
    if not provider_id or provider_id not in service.registry.by_id:
        raise ProviderConfigError(
            "OpenKai default provider is not in the pinned provider registry"
        )
    return provider_id


def _resolved_child_env_from_snapshot(
    service: ProviderConfigService,
    provider_id: str,
    resolved: dict[str, str],
) -> dict[str, str]:
    descriptor = service.registry.by_id.get((provider_id or "").strip())
    if descriptor is None:
        raise ProviderConfigError("provider is not in the pinned OpenKai registry")
    credential_state = service.auth_credential_state(descriptor["id"])
    if credential_state == "oauth_present_held":
        raise ProviderConfigError(
            "OpenKai OAuth execution is held pending cross-process credential locking"
        )
    if credential_state == "api_key_authority_conflict":
        raise ProviderConfigError(
            "OpenKai auth store contains an API-key authority conflict"
        )
    if credential_state == "unverified":
        raise ProviderConfigError("OpenKai credential-store metadata is unverified")
    env_keys = descriptor["env_keys"]
    if not env_keys:
        raise ProviderConfigError(
            "OAuth-only provider execution is held for API-key-only UAT"
        )
    if descriptor["oauth"] and env_keys[0] not in resolved:
        raise ProviderConfigError(
            "OpenKai OAuth env aliases are held for API-key-only UAT"
        )
    child_env = {key: resolved[key] for key in env_keys if resolved.get(key)}
    if not child_env:
        # Returning an empty mapping is unsafe: OpenKai would continue to its
        # workspace dotenv fallback after the runner scrubbed inherited values.
        raise ProviderConfigError("provider is not configured in the OpenKai authority")
    return child_env


def resolved_child_env(provider_id: str) -> dict[str, str]:
    """Selected provider's exact source key/value for one child, in memory.

    No unrelated provider is returned and a compatibility alias keeps its exact
    name. The caller must remove every name in :func:`provider_env_keys` from
    inherited env before applying this map, because OpenKai gives an already-set
    process variable precedence over the owner-private file.
    """
    service = ProviderConfigService()
    return _resolved_child_env_from_snapshot(
        service, provider_id, service.resolved_environment()
    )


def resolved_default_child_env(*, expected_provider: str | None = None) -> dict[str, str]:
    """Resolve OpenKai's configured/default route from its global file only.

    A blank KOS model deliberately omits ``--provider`` and ``--model``.  This
    seam still prevents process/workspace dotenv fallthrough: it validates the
    optional OpenKai route variables in ``~/.openkai/.env``, requires the exact
    selected provider credential from that same snapshot, and returns only that
    credential plus an explicit provider route. If no provider is configured,
    the hash-pinned registry's upstream default is selected and named explicitly
    so the caller can prove it matches the earlier metadata-only entitlement check.
    """
    service = ProviderConfigService()
    _data, _lines, values = service._read_parsed()

    provider_id = values.get("OPENKAI_PROVIDER", service.registry.default_provider)
    if not provider_id or provider_id not in service.registry.by_id:
        raise ProviderConfigError(
            "OpenKai default provider is not in the pinned provider registry"
        )
    if expected_provider is not None and provider_id != expected_provider:
        # The caller entitled the metadata-only route before asking for a secret.
        # Refuse a route change from this same snapshot before selecting/returning
        # any credential to the child boundary.
        raise ProviderConfigConflict(
            "OpenKai default provider changed after entitlement validation"
        )

    model_present = "OPENKAI_MODEL" in values
    model = values.get("OPENKAI_MODEL", "")
    if model_present and not _OPENKAI_MODEL_VALUE.fullmatch(model):
        raise ProviderConfigError("OpenKai default model syntax is invalid")

    resolved = service._resolved_environment_from_values(values)
    child_env = _resolved_child_env_from_snapshot(service, provider_id, resolved)
    child_env["OPENKAI_PROVIDER"] = provider_id
    if model_present:
        child_env["OPENKAI_MODEL"] = model
    return child_env
