"""Bounded explicit-argv engine adapter; no SDK or resource provisioning."""

import hashlib
import json
import math
import os
import selectors
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
    """One monotonic absolute budget; each declared phase keeps its own cap.

    With reserve, the last `reserve` seconds belong to cleanup only. enter_phase
    fixes a phase window once at entry; child_timeout hands out only what is
    left of it (never a fresh phase allowance per child). cleanup_timeout leaves
    the caller's kill allowance inside the absolute budget.
    """

    def __init__(self, absolute_timeout, *, clock=time.monotonic, reserve=0):
        self.clock = clock
        _positive_timeout(absolute_timeout)
        if type(reserve) not in (int, float) or not math.isfinite(reserve) or not 0 <= reserve <= absolute_timeout:
            raise ValueError("cleanup reserve must fit inside the absolute budget")
        self.expires = clock() + absolute_timeout
        self.work_expires = self.expires - reserve
        self.phase_expires = self.work_expires
        self.cleanup_expires = None

    def phase_timeout(self, declared_timeout):
        cap = _positive_timeout(declared_timeout)
        remaining = self.expires - self.clock()
        if remaining <= 0:
            raise TimeoutError("absolute build deadline expired")
        return min(cap, remaining)

    def enter_phase(self, declared_timeout):
        cap = _positive_timeout(declared_timeout)
        now = self.clock()
        if now >= self.work_expires:
            raise TimeoutError("work deadline expired")
        self.phase_expires = min(now + cap, self.work_expires)

    def child_timeout(self, kill_seconds=0):
        """What is left of the fixed phase window, minus the child's own TERM/KILL wait."""
        left = min(self.phase_expires, self.work_expires) - self.clock() - kill_seconds
        if left <= 0:
            raise TimeoutError("phase window spent")
        return left

    def enter_cleanup(self, declared_timeout):
        """Fix cleanup's own window once, at its entry, inside the absolute deadline."""
        self.cleanup_expires = min(self.clock() + _positive_timeout(declared_timeout), self.expires)

    def cleanup_timeout(self, kill_seconds):
        limit = self.expires if self.cleanup_expires is None else self.cleanup_expires
        return limit - self.clock() - kill_seconds


class OutputAccount:
    """One cumulative retained-output budget (stdout + stderr + journals).

    The last `reserve` bytes are usable only after cleanup(): overflowing work
    output can never starve removal/absence evidence. A work overflow is sticky.
    """

    def __init__(self, limit, reserve=0):
        if type(limit) is not int or limit <= 0 or type(reserve) is not int or not 0 <= reserve < limit:
            raise ValueError("positive integer output budget and smaller reserve required")
        self.limit, self.reserve, self.used = limit, reserve, 0
        self.overflow = self.cleaning = False

    def cleanup(self):
        self.cleaning = True

    def take(self, count):
        ceiling = self.limit if self.cleaning else self.limit - self.reserve
        if (self.overflow and not self.cleaning) or self.used + count > ceiling:
            if not self.cleaning:
                self.overflow = True
            raise OverflowError("output budget exceeded")
        self.used += count


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


def _drain(process, sinks, account, expires, clock, argv, timeout):
    """Copy both pipes until EOF under one cumulative account and the deadline."""
    total = 0
    with selectors.DefaultSelector() as selector:
        for stream in sinks:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = expires - clock()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(argv), timeout)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                try:
                    account.take(len(chunk))
                except OverflowError:
                    raise OverflowError("operation output budget exceeded") from None
                total += len(chunk)
                sinks[key.fileobj].write(chunk)
    return total


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
    output_limit=None,
    kill_seconds=20,
    account=None,
):
    """Run one explicit argv under one deadline.

    With output_limit, stdout+stderr are drained together while the process
    runs against ONE cumulative byte budget; the first excess byte stops the
    process group and raises OverflowError. With account (an OutputAccount
    shared by a whole lane) the same drain charges that account, and the journal
    rows are charged too. kill_seconds is the total budget for the TERM then
    KILL waits after a failure.
    """
    if not isinstance(argv, (list, tuple)):
        raise ValueError("explicit argv sequence required")
    argv = tuple(argv)
    if not argv or any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise ValueError("bounded explicit argv required")
    _positive_timeout(timeout)
    _positive_timeout(kill_seconds)
    if output_limit is not None and (type(output_limit) is not int or output_limit <= 0):
        raise ValueError("positive integer output budget required")
    if account is not None and (output_limit is not None or not isinstance(account, OutputAccount)):
        raise ValueError("one output account or one per-call limit, not both")
    shared = account is not None
    if output_limit is not None:
        account = OutputAccount(output_limit)
    environment = dict(environment)
    start = clock()
    process = None
    code = None
    failure = None
    journal_path = Path(str(stdout) + ".operation.jsonl")
    with (
        Path(stdout).open("xb") as out,
        Path(stderr).open("xb") as err,
        journal_path.open("x", encoding="utf-8", newline="\n") as journal,
    ):

        def observe(row):
            line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            if shared and row.get("status") == "started":
                # the closing row is reserved with the opening one (bounded size)
                account.take(len(line.encode()) + 512)
            journal.write(line)
            journal.flush()
            os.fsync(journal.fileno())

        observe(
            {
                "status": "started",
                "argv": list(argv),
                "environment_names": sorted(environment),
                "started_at_unix_ns": time.time_ns(),
                "timeout_ns": int(timeout * 1_000_000_000),
            }
        )
        try:
            piped = account is not None
            process = popen(
                argv,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if piped else out,
                stderr=subprocess.PIPE if piped else err,
                start_new_session=True,
            )
            if piped:
                _drain(process, {process.stdout: out, process.stderr: err},
                       account, start + timeout, clock, argv, timeout)
                code = process.wait(timeout=max(start + timeout - clock(), 0.001))
            else:
                code = process.wait(timeout=timeout)
        except BaseException as error:
            failure = type(error).__name__
            if process is not None:
                kill_started = clock()
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    code = process.wait(timeout=kill_seconds / 2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    code = process.wait(timeout=max(kill_seconds - (clock() - kill_started), 0.001))
            raise
        finally:
            if process is not None and account is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            observe(
                {
                    "status": "finished" if failure is None and code == 0 else "failed",
                    "exit": code,
                    "error": failure,
                    "finished_at_unix_ns": time.time_ns(),
                    "elapsed_ns": int((clock() - start) * 1_000_000_000),
                }
            )
    return {
        "argv": list(argv),
        "exit": code,
        "elapsed_seconds": clock() - start,
        "stdout": str(stdout),
        "stderr": str(stderr),
        "journal": str(journal_path),
    }


def build(
    engine,
    source,
    iidfile,
    epoch,
    store,
    runroot,
    environment,
    timeout,
    stdout,
    stderr,
    *,
    resources,
):
    argv = build_argv(engine, source, iidfile, epoch)
    argv = [*argv[:2], *resource_argv(resources), *argv[2:]]
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
        import stat
        from . import _owned_remove

        failures = []
        for path, expected in self.created.items():
            try:
                current = path.stat(follow_symlinks=False)
                if (
                    self.identity(current) != expected
                    or current.st_uid != os.getuid()
                    or not stat.S_ISDIR(current.st_mode)
                ):
                    raise ValueError("owned root identity changed")
                # the one descriptor-safe removal, also used inside the user namespace
                _owned_remove.remove(path, expected[0], expected[1])
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


def resource_argv(resources):
    """Use the admitted integer limits; disk enforcement is a separate observation."""
    for key in ("cpu_millicores", "memory_bytes", "disk_bytes"):
        if type(resources.get(key)) is not int or resources[key] <= 0:
            raise ValueError("explicit positive resource limit required: " + key)
    return cpu_memory_argv(resources["cpu_millicores"], resources["memory_bytes"])


def cpu_memory_argv(cpu_millicores, memory_bytes):
    """The one CPU/memory flag formatting; it claims nothing about disk."""
    for key, value in (("cpu_millicores", cpu_millicores), ("memory_bytes", memory_bytes)):
        if type(value) is not int or value <= 0:
            raise ValueError("explicit positive resource limit required: " + key)
    return [
        "--cpu-period",
        "1000000",
        "--cpu-quota",
        str(cpu_millicores * 1000),
        "--memory",
        str(memory_bytes),
    ]
