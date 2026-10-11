"""Bounded explicit-argv engine adapter; no SDK or resource provisioning."""

from contextlib import contextmanager
import hashlib
import math
import os
import signal
import subprocess
import time
from pathlib import Path
from .policy import build_argv


def _positive_timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("finite positive operation budget required")
    return value


class Deadline:
    """One monotonic absolute budget; each declared phase keeps its own cap."""

    def __init__(self, absolute_timeout, *, clock=time.monotonic):
        self.clock = clock
        self.expires = clock() + _positive_timeout(absolute_timeout)

    def phase_timeout(self, declared_timeout):
        cap = _positive_timeout(declared_timeout)
        remaining = self.expires - self.clock()
        if remaining <= 0:
            raise TimeoutError("absolute build deadline expired")
        return min(cap, remaining)


def executable_bytes(path, expected):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("physical pinned executable required")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("engine executable changed")
    return raw


def require_fresh_paths(paths):
    resolved = [Path(p).resolve() for p in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("shared mutable build root")
    if any(a.is_relative_to(b) for a in resolved for b in resolved if a != b):
        raise ValueError("nested mutable build roots")
    for path in resolved:
        if path.exists():
            raise ValueError("fresh mutable root required")
    return resolved


def run_operation(
    argv,
    cwd,
    environment,
    timeout,
    stdout,
    stderr,
    *,
    popen=subprocess.Popen,
    clock=time.monotonic,
):
    if (
        not argv
        or any(not isinstance(a, str) or "\x00" in a for a in argv)
        or type(timeout) not in [int, float]
        or timeout <= 0
    ):
        raise ValueError("bounded explicit argv required")
    _positive_timeout(timeout)
    start = clock()
    process = None
    with Path(stdout).open("xb") as out, Path(stderr).open("xb") as err:
        try:
            process = popen(
                argv,
                cwd=str(cwd),
                env=environment,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            code = process.wait(timeout=timeout)
        except BaseException:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=10)
            raise
    return {
        "argv": list(argv),
        "exit": code,
        "elapsed_seconds": clock() - start,
        "stdout": str(stdout),
        "stderr": str(stderr),
    }


def build(
    engine, source, iidfile, epoch, store, runroot, environment, timeout, stdout, stderr
):
    argv = build_argv(engine, source, iidfile, epoch)
    argv = [argv[0], "--root", str(store), "--runroot", str(runroot), *argv[1:]]
    return run_operation(argv, source, environment, timeout, stdout, stderr)


class OwnedRoots:
    """Track only roots this adapter creates; replacement or foreign ownership refuses."""

    def __init__(self, paths):
        self.paths = require_fresh_paths(paths)
        if not self.paths:
            raise ValueError("owned mutable roots required")
        self.created = {}

    @staticmethod
    def identity(value):
        return value.st_dev, value.st_ino, value.st_uid

    def create(self):
        if self.created:
            raise ValueError("roots already allocated")
        for path in self.paths:
            path.mkdir(mode=0o700)
            self.created[path] = self.identity(path.stat(follow_symlinks=False))
        return self

    def cleanup(self):
        import shutil
        import stat

        failures = []
        if not shutil.rmtree.avoids_symlink_attacks:
            return {
                "complete": False,
                "remaining_owned_resources": [str(p) for p in self.created],
                "errors": ["descriptor-safe cleanup unavailable"],
            }
        for path, expected in self.created.items():
            try:
                current = path.stat(follow_symlinks=False)
                if (
                    self.identity(current) != expected
                    or current.st_uid != os.getuid()
                    or not stat.S_ISDIR(current.st_mode)
                ):
                    raise ValueError("owned root identity changed")
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                with _directory_descriptor(descriptor) as owned_fd:
                    if self.identity(os.fstat(owned_fd)) != expected:
                        raise ValueError("owned root changed before cleanup")
                    for entry in os.scandir(owned_fd):
                        observed = os.stat(
                            entry.name, dir_fd=owned_fd, follow_symlinks=False
                        )
                        if stat.S_ISDIR(observed.st_mode):
                            shutil.rmtree(entry.name, dir_fd=owned_fd)
                        else:
                            os.unlink(entry.name, dir_fd=owned_fd)
                    if self.identity(path.stat(follow_symlinks=False)) != expected:
                        raise ValueError("owned root replaced during cleanup")
                    path.rmdir()
            except (OSError, ValueError) as error:
                failures.append({"path": str(path), "reason": type(error).__name__})
        remaining = [
            str(path) for path in self.created if path.exists() or path.is_symlink()
        ]
        return {
            "complete": not failures and not remaining,
            "remaining_owned_resources": remaining,
            "errors": failures,
        }


@contextmanager
def _directory_descriptor(descriptor):
    try:
        yield descriptor
    finally:
        os.close(descriptor)
