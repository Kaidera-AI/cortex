"""Finite persistent Bash recording; captured streams are private, not exported."""
from __future__ import annotations

import hashlib
import os
import selectors
import signal
import subprocess
import time
import uuid

from custody import Refusal, bound_file, public_data, require


class PersistentRecorder:
    def __init__(self, argv, cwd, env, *, popen=subprocess.Popen):
        require(isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv), "recorder_argv")
        self.process = popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self.used = set()
        self.closed = False

    def run(self, name, literal_row, *, timeout=60, observer, public_contract=None):
        try:
            return self._run(name, literal_row, timeout=timeout, observer=observer, public_contract=public_contract)
        except BaseException:
            self.close()
            raise

    def _run(self, name, literal_row, *, timeout=60, observer, public_contract):
        require(name not in self.used, "literal_replay_refused")
        require(type(timeout) is int and 0 < timeout <= 900, "literal_deadline")
        literal = bound_file(literal_row, read=True, limit=1024 * 1024)
        require(self.process.poll() is None, "persistent_shell_already_failed")
        self.used.add(name)
        marker = ("RUN8_ACK_" + uuid.uuid4().hex).encode()
        # No if/function/AND/OR wrapper: errexit belongs to the literal Bash.
        payload = b"set -euo pipefail\n" + literal + b"\nprintf '%s\\n' '" + marker + b"'\n"
        started = time.monotonic()
        offset = 0
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        ack = False
        while self.selector.get_map() and time.monotonic() - started < timeout:
            for key, _ in self.selector.select(min(0.25, max(0, timeout - (time.monotonic() - started)))):
                if key.data == "stdin":
                    try:
                        offset += os.write(key.fileobj.fileno(), payload[offset:offset + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        self.selector.unregister(key.fileobj)
                        continue
                    if offset == len(payload):
                        self.selector.unregister(key.fileobj)
                    continue
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    continue
                streams[key.data].extend(chunk)
                require(sum(len(v) for v in streams.values()) <= 2 * 1024 * 1024, "literal_capture_bound")
            if marker + b"\n" in streams["stdout"]:
                ack = True
                break
        if not ack:
            self.close()
            raise Refusal("literal_failed_or_missing_ack")
        require(self.process.poll() in {None, 0}, "literal_nonzero_transport")
        # Observer must return allowlisted nonsecret facts; raw streams/hash are
        # never included in a public receipt. A post-effect assertion may refuse.
        facts = observer(bytes(streams["stdout"]).replace(marker + b"\n", b""), bytes(streams["stderr"]))
        public_data(facts)
        # Explicit scalar schemas; arbitrary capture/text cannot become a fact.
        contract = public_contract if public_contract is not None else {
            "initialized": {"type": "boolean"}, "effect": {"type": "enum", "values": ["effect-ok"]}}
        require(isinstance(facts, dict) and isinstance(contract, dict) and set(facts) <= set(contract),
                "recorder_public_fields")
        for key, value in facts.items():
            rule = contract[key]
            kind = rule.get("type")
            valid = ((kind == "boolean" and type(value) is bool)
                     or (kind == "integer" and type(value) is int and 0 <= value <= rule.get("max", 0))
                     or (kind == "enum" and isinstance(value, str) and value in rule.get("values", [])))
            require(valid, "recorder_public_value")
        return {"literal_sha256": literal_row["sha256"], "ACK_observed": True,
                "persistent_same_Bash": True, "seconds": time.monotonic() - started,
                "post_effect": facts}

    def _signal_group(self, sig):
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin can return EPERM for an empty/exited process group. Verify
            # that it has no executing member; never silently ignore a live one.
            self.process.poll()
            rows = subprocess.check_output(["ps", "-axo", "pid=,pgid=,uid=,stat="], text=True).splitlines()
            executing = [r for r in rows if len(r.split()) == 4
                         and r.split()[1] == str(self.process.pid) and not r.split()[3].startswith("Z")]
            require(not executing, "recorder_group_stop_refused")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.poll()  # Reap an exited shell before signalling its group.
            self._signal_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            # Also stop children when Bash has already exited or ignored TERM.
            self._signal_group(signal.SIGKILL)
            self.process.wait(timeout=2)
        finally:
            self.selector.close()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except BrokenPipeError:
                        pass
