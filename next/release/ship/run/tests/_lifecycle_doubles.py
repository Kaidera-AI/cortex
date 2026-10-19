"""Named source-only doubles for the S5 serve lifecycle (no Podman, unlock or secret).

RecordDouble stands in for the ONE installer record (install.sh `record_object`, Nix); it
keeps the same append-or-replace-by-name shape. EngineDouble stands in for the container
engine; it hands out deterministic IDs and never runs a process. Both write to one shared
event log so a test can see that each create is recorded before the next effect.
"""
import contextlib
import os

BOOTSTRAP = {"kind": "bootstrap", "name": "cortex-v2-bootstrap", "id": "s4-receipt-fixture"}


class RecordDouble:
    def __init__(self, log, *, objects=(), writable=True):
        self.log = log
        self.writable = writable
        self._objects = [dict(o) for o in objects]

    def objects(self):
        return [dict(o) for o in self._objects]

    def record(self, kind, name, ident):
        assert self.writable, "a read-only record was written"
        self._objects = [o for o in self._objects if o["name"] != name] + [
            {"kind": kind, "name": name, "id": ident}]
        self.log.append(("record", kind, name, ident))

    def forget(self, name):
        assert self.writable, "a read-only record was written"
        self._objects = [o for o in self._objects if o["name"] != name]
        self.log.append(("forget", name))


class EngineDouble:
    """Objects keyed by (kind, name) -> {"id", "state"}; `fail_on` makes one create fail."""

    def __init__(self, log, *, baseline=(), fail_on=None, fail_remove=None):
        self.log = log
        self.objects = {key: dict(value) for key, value in dict(baseline).items()}
        self.fail_on = fail_on
        self.fail_remove = fail_remove
        self.unlock_reads = []
        self._serial = 0

    def lookup(self, kind, name):
        found = self.objects.get((kind, name))
        return found["id"] if found else None

    def create(self, argv, *, unlock_fd=None, deadline):
        kind = "pod" if argv[:3] == ["podman", "pod", "create"] else "container"
        name = argv[argv.index("--name") + 1]
        if unlock_fd is not None:
            # Synthetic nonsecret bytes: prove the descriptor reached the create, nothing more.
            self.unlock_reads.append((name, os.read(unlock_fd, 64)))
        self.log.append(("create", kind, name, tuple(argv), unlock_fd is not None))
        if self.fail_on == name:
            raise OSError("engine create failed (double)")
        self._serial += 1
        ident = f"{kind}-{self._serial:04d}"
        self.objects[(kind, name)] = {"id": ident, "state": "running"}
        return ident

    def stop(self, kind, ident, *, deadline):
        for (k, name), value in self.objects.items():
            if k == kind and value["id"] == ident:
                value["state"] = "stopped"
                self.log.append(("stop", kind, name, ident))
                return
        raise OSError("no such object (double)")

    def remove(self, kind, ident, *, deadline):
        for (k, name), value in list(self.objects.items()):
            if k == kind and value["id"] == ident:
                if self.fail_remove == name:
                    raise OSError("engine remove failed (double)")
                del self.objects[(k, name)]
                self.log.append(("remove", kind, name, ident))
                return
        raise OSError("no such object (double)")


@contextlib.contextmanager
def unlock_pipe(payload=b"S5-SYNTHETIC-NONSECRET-UNLOCK-32"):
    """An anonymous pipe read end holding synthetic bytes, like a caller's fresh unlock."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, payload)
        os.close(write_end)
        write_end = None
        yield read_end
    finally:
        if write_end is not None:
            os.close(write_end)
        os.close(read_end)
