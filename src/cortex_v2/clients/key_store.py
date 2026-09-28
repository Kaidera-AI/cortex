"""Private, installation-bound credential storage; secrets never use a shell."""

from __future__ import annotations

import datetime
import json
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal


class KeyStoreError(Exception):
    """A credential could not be stored or read safely."""

@dataclass(frozen=True, slots=True)
class KeyMetadata:
    managed_by: str
    expires_at: str

    def due_state(self, now: datetime.datetime) -> Literal["due", "expired"] | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise KeyStoreError("credential status clock must include a timezone")
        expiry = datetime.datetime.fromisoformat(self.expires_at)
        if expiry <= now:
            return "expired"
        if expiry - now <= datetime.timedelta(days=30):
            return "due"
        return None


@dataclass(frozen=True, slots=True)
class DueKey:
    project: str
    name: str
    managed_by: str
    expires_at: str
    state: Literal["due", "expired"]


@dataclass(frozen=True, slots=True)
class _KeyRecord:
    token: str = field(repr=False)
    metadata: KeyMetadata


def _metadata(managed_by: str, expires_at: str) -> KeyMetadata:
    if managed_by not in ("user", "kos", "openkai") or not isinstance(expires_at, str):
        raise KeyStoreError("credential requires an API-issued manager and expiry")
    try:
        expiry = datetime.datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise KeyStoreError("credential expiry must be an API-issued ISO timestamp") from exc
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise KeyStoreError("credential expiry must include a timezone")
    return KeyMetadata(managed_by, expires_at)


def _record(data: bytes) -> _KeyRecord:
    try:
        parsed = json.loads(data)
        if not isinstance(parsed, dict) or set(parsed) != {"token", "managed_by", "expires_at"}:
            raise ValueError("missing credential metadata")
        if not isinstance(parsed["token"], str) or not parsed["token"]:
            raise ValueError("missing credential")
        metadata = _metadata(parsed["managed_by"], parsed["expires_at"])
        return _KeyRecord(parsed["token"], metadata)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise KeyStoreError("credential record is invalid") from exc



def _label(value: str) -> str:
    if not value or "/" in value or "\\" in value or "\x00" in value or value in (".", ".."):
        raise KeyStoreError("credential identity contains an unsafe path component")
    return value


def _private_directory(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise KeyStoreError("credential directory must not contain a symlink")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise KeyStoreError("cannot create private credential directory") from exc
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise KeyStoreError("credential directory must be owner-only mode 0700")


def _check_file(directory: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise KeyStoreError("credential file must be an owner-only regular file (0600)")
    return True


def _read_file(directory: int, filename: str) -> _KeyRecord | None:
    if not _check_file(directory, filename):
        return None
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise KeyStoreError("credential file changed during read")
        raw = handle.read(4097)
        if len(raw) > 4096:
            raise KeyStoreError("credential record exceeds the safe size limit")
        return _record(raw)


class _FileStore:
    def __init__(self, installation: str, root: Path | None) -> None:
        base = root if root is not None else Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        ) / "cortex" / "keys"
        self.root = Path(os.path.abspath(base))
        self.installation = _label(installation)

    @contextmanager
    def _installation_dir(self) -> Iterator[int]:
        _private_directory(self.root)
        _private_directory(self.root / self.installation)
        rootfd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            installationfd = os.open(
                self.installation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=rootfd,
            )
            try:
                for fd in (rootfd, installationfd):
                    info = os.fstat(fd)
                    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise KeyStoreError("credential directory must be owner-only mode 0700")
                yield installationfd
            finally:
                os.close(installationfd)
        finally:
            os.close(rootfd)

    @contextmanager
    def _dir(self, project: str) -> Iterator[int]:
        with self._installation_dir() as installationfd:
            _private_directory(self.root / self.installation / _label(project))
            projectfd = os.open(
                project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=installationfd,
            )
            try:
                info = os.fstat(projectfd)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise KeyStoreError("credential directory must be owner-only mode 0700")
                yield projectfd
            finally:
                os.close(projectfd)

    def get(self, project: str, name: str) -> _KeyRecord | None:
        filename = f"{_label(name)}.key"
        try:
            with self._dir(project) as directory:
                return _read_file(directory, filename)
        except (OSError, UnicodeError) as exc:
            raise KeyStoreError("cannot safely read credential file") from exc

    def metadata_entries(self) -> list[tuple[str, str, KeyMetadata]]:
        entries: list[tuple[str, str, KeyMetadata]] = []
        try:
            with self._installation_dir() as installation:
                for project in sorted(os.listdir(installation)):
                    _label(project)
                    with self._dir(project) as directory:
                        for filename in sorted(os.listdir(directory)):
                            if filename.startswith("."):
                                temporary = (
                                    len(filename) == 33
                                    and all(char in "0123456789abcdef" for char in filename[1:])
                                )
                                if temporary and _check_file(directory, filename):
                                    continue
                                raise KeyStoreError("unexpected private credential directory entry")
                            if not filename.endswith(".key"):
                                raise KeyStoreError("unexpected private credential directory entry")
                            name = _label(filename[:-4])
                            record = _read_file(directory, filename)
                            if record is None:
                                raise KeyStoreError("credential disappeared during status")
                            entries.append((project, name, record.metadata))
        except OSError as exc:
            raise KeyStoreError("cannot safely enumerate credentials") from exc
        return entries

    def put(self, project: str, name: str, record: _KeyRecord) -> None:
        filename = f"{_label(name)}.key"
        try:
            with self._dir(project) as directory:
                _check_file(directory, filename)
                temporary = f".{secrets.token_hex(16)}"
                try:
                    fd = os.open(
                        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600, dir_fd=directory,
                    )
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(json.dumps({
                            "token": record.token,
                            "managed_by": record.metadata.managed_by,
                            "expires_at": record.metadata.expires_at,
                        }, separators=(",", ":")).encode("utf-8"))
                        handle.flush()
                        os.fsync(handle.fileno())
                    _check_file(directory, filename)
                    os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            raise KeyStoreError("cannot safely store credential") from exc

    def delete(self, project: str, name: str) -> None:
        filename = f"{_label(name)}.key"
        try:
            with self._dir(project) as directory:
                if _check_file(directory, filename):
                    os.unlink(filename, dir_fd=directory)
                    os.fsync(directory)
        except OSError as exc:
            raise KeyStoreError("cannot safely remove credential") from exc


def _is_linux() -> bool:
    return sys.platform == "linux"


class KeyStore:
    def __init__(
        self, installation: str, *, root: Path | None = None,
        backend: str | None = None,
    ) -> None:
        self.installation = _label(installation)
        if not _is_linux():
            raise KeyStoreError("private file credential storage requires Linux")
        if backend not in (None, "file"):
            raise KeyStoreError("unsupported credential storage backend")
        self._native = _FileStore(self.installation, root)
        self.backend = "file"

    def get(self, project: str, name: str) -> str | None:
        record = self._native.get(_label(project), _label(name))
        return record.token if record is not None else None

    def metadata(self, project: str, name: str) -> KeyMetadata | None:
        record = self._native.get(_label(project), _label(name))
        return record.metadata if record is not None else None

    def due(self, *, now: datetime.datetime | None = None) -> list[DueKey]:
        if not _is_linux() or self.backend != "file":
            raise KeyStoreError("local credential status requires Linux private file storage")
        clock = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise KeyStoreError("credential status clock must include a timezone")
        due_keys: list[DueKey] = []
        for project, name, metadata in self._native.metadata_entries():
            state = metadata.due_state(clock)
            if state is not None:
                due_keys.append(DueKey(
                    project, name, metadata.managed_by, metadata.expires_at, state,
                ))
        return due_keys

    def put(
        self, project: str, name: str, token: str, *,
        managed_by: str, expires_at: str,
    ) -> None:
        _label(project)
        _label(name)
        if not token or "\x00" in token:
            raise KeyStoreError("credential must not be empty or contain NUL")
        self._native.put(project, name, _KeyRecord(token, _metadata(managed_by, expires_at)))

    def delete(self, project: str, name: str) -> None:
        self._native.delete(_label(project), _label(name))
