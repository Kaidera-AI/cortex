"""Read per-project Cortex ServiceAuth credentials from protected local files."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Iterator


TOKEN_PATTERN = re.compile(r"ctx1_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}")
PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")
AGENT_PATTERN = re.compile(r"[a-z][a-z0-9_-]{1,31}")
DEFAULT_INSTALLATION = "KOS-Cortex"


class CortexCredentialUnavailable(RuntimeError):
    """No safe credential exists for the requested canonical identity."""


def token_root() -> Path:
    configured = os.environ.get("CORTEX_AUTH_TOKEN_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".kaidera-os" / "cortex-auth" / DEFAULT_INSTALLATION


def agent_base(value: str) -> str:
    return re.split(r"[:@]", str(value or "").strip().lower(), maxsplit=1)[0]


def _canonical_identity(project: str, agent: str) -> tuple[str, str]:
    project = str(project or "").strip().lower()
    agent = agent_base(agent)
    if not PROJECT_PATTERN.fullmatch(project) or not AGENT_PATTERN.fullmatch(agent):
        raise CortexCredentialUnavailable("Canonical Cortex project and agent are required")
    return project, agent


def token_path(project: str, agent: str, *, root: Path | None = None) -> Path:
    project, agent = _canonical_identity(project, agent)
    return (root or token_root()) / project / f"{agent}.token"


def _private_directory(path: Path, *, owner_uid: int | None = None) -> int:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CortexCredentialUnavailable("Cortex credential directory is unavailable") from exc
    process_uid = os.getuid()
    expected_uid = process_uid if process_uid != 0 else (owner_uid if owner_uid is not None else info.st_uid)
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != expected_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise CortexCredentialUnavailable("Cortex credential directory is not private")
    return expected_uid


def read_token(project: str, agent: str, *, root: Path | None = None) -> str:
    root = (root or token_root()).expanduser()
    path = token_path(project, agent, root=root)
    owner_uid = _private_directory(root)
    _private_directory(path.parent, owner_uid=owner_uid)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > 128
        ):
            raise CortexCredentialUnavailable("Cortex credential file is not private")
        with os.fdopen(descriptor, "r", encoding="ascii", closefd=False) as handle:
            token = handle.read(129).strip()
    except CortexCredentialUnavailable:
        raise
    except (OSError, UnicodeError) as exc:
        raise CortexCredentialUnavailable("Cortex credential is unavailable") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if not TOKEN_PATTERN.fullmatch(token):
        raise CortexCredentialUnavailable("Cortex credential has an invalid format")
    return token


def _project_candidates(root: Path, preferred: str, agent: str) -> Iterator[str]:
    seen: set[str] = set()
    for project in (preferred, os.environ.get("CORTEX_AUTH_DEFAULT_PROJECT", "")):
        project = str(project or "").strip().lower()
        if PROJECT_PATTERN.fullmatch(project) and project not in seen:
            seen.add(project)
            yield project
    try:
        owner_uid = _private_directory(root)
        directories = sorted(
            item
            for item in root.iterdir()
            if item.is_dir() and not item.is_symlink() and item.lstat().st_uid == owner_uid
        )
    except (OSError, CortexCredentialUnavailable):
        return
    for directory in directories:
        project = directory.name
        if project not in seen and PROJECT_PATTERN.fullmatch(project):
            seen.add(project)
            yield project


def resolve_token(
    project: str = "",
    agent: str = "",
    *,
    default_project: str = "",
    default_agent: str = "",
    root: Path | None = None,
) -> str:
    """Return the exact identity token, or a deterministic default for global calls."""
    configured = os.environ.get("CORTEX_API_BEARER_TOKEN", "").strip()
    if configured:
        if not TOKEN_PATTERN.fullmatch(configured):
            raise CortexCredentialUnavailable("CORTEX_API_BEARER_TOKEN has an invalid format")
        return configured

    root = (root or token_root()).expanduser()
    requested_project = str(project or "").strip().lower()
    if requested_project == "_system":
        requested_project = ""
    requested_agent = agent_base(agent or default_agent)
    if requested_project:
        return read_token(requested_project, requested_agent, root=root)

    for candidate in _project_candidates(root, default_project, requested_agent):
        try:
            return read_token(candidate, requested_agent, root=root)
        except CortexCredentialUnavailable:
            continue
    raise CortexCredentialUnavailable("No Cortex credential exists for the requested identity")


try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - shell-only installations
    httpx = None


if httpx is not None:
    class CortexServiceAuth(httpx.Auth):
        """Inject a freshly-read opaque token selected from request identity headers."""

        def __init__(self, *, default_project: str = "", default_agent: str = "") -> None:
            self.default_project = default_project
            self.default_agent = default_agent

        def auth_flow(self, request):
            request.headers.pop("X-Cortex-Admin-Token", None)
            try:
                token = read_token(
                    request.headers.get("X-Project", "") or self.default_project,
                    request.headers.get("X-Agent-Name", "") or self.default_agent,
                )
            except CortexCredentialUnavailable as exc:
                raise httpx.LocalProtocolError(str(exc), request=request) from exc
            request.headers["Authorization"] = f"Bearer {token}"
            yield request
