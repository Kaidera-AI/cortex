"""Finite persistent Bash recording; captured streams are private, not exported."""
from __future__ import annotations

import hashlib
import os
import selectors
import subprocess
import time
import uuid

from custody import Refusal, bound_file, public_data, require


class PersistentRecorder:
    def __init__(self, argv, cwd, env, *, popen=subprocess.Popen):
        require(isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv), "recorder_argv")
        self.process = popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self.used = set()

    def run(self, name, literal_row, *, timeout=60, observer):
        try:
            return self._run(name, literal_row, timeout=timeout, observer=observer)
        except BaseException:
            self.close()
            raise

    def _run(self, name, literal_row, *, timeout=60, observer):
        require(name not in self.used, "literal_replay_refused")
        require(type(timeout) is int and 0 < timeout <= 900, "literal_deadline")
        literal = bound_file(literal_row, read=True, limit=1024 * 1024)
        require(self.process.poll() is None, "persistent_shell_already_failed")
        self.used.add(name)
        marker = ("RUN8_ACK_" + uuid.uuid4().hex).encode()
        # No if/function/AND/OR wrapper: errexit belongs to the literal Bash.
        payload = b"set -euo pipefail\n" + literal + b"\nprintf '%s\\n' '" + marker + b"'\n"
        started = time.monotonic()
        self.process.stdin.write(payload)
        self.process.stdin.flush()
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        ack = False
        while self.selector.get_map() and time.monotonic() - started < timeout:
            for key, _ in self.selector.select(min(0.25, max(0, timeout - (time.monotonic() - started)))):
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
        return {"literal_sha256": literal_row["sha256"], "ACK_observed": True,
                "persistent_same_Bash": True, "seconds": time.monotonic() - started,
                "post_effect": facts}

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=2)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        self.selector.close()
