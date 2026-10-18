"""Shared S4 source fixtures (NONSECRET, below IO only).

1. Stages the exact ship install bytes into a temporary `cortex_ship.install` package, the API
   image layout (recipes/api/Containerfile), so Core's owner.run imports the actual Nix
   provision_sequence. Only the two package markers are test-layout files; each staged file is
   hash-checked against its source.
2. `composed(render, config)`: Finn's named composition doubles, used unchanged from
   cortex_core/runtime/tests/test_installer_composition.py: SourceAdapters (unlock sentinel 99,
   memory record, non-crypto cipher, clock, entropy), NonsecretSQLFixture (below-driver rows),
   the CA digest, the psycopg status enum and seed entropy, plus ONE clock shared with render.
3. `BootstrapEngine`: the S5 EngineDouble plus the one foreground `run` (records argv and the
   bytes read from the inherited descriptor) and volume lookup. It records argv/input only; it
   never initialises a database, starts a server or reaches a network.
"""
import atexit
from contextlib import ExitStack, contextmanager
import hashlib
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from uuid import UUID

HERE = pathlib.Path(__file__).resolve().parent
NEXT = HERE.parents[3]
INSTALL = NEXT / "release" / "ship" / "install"
SOURCES = {"provision.py": INSTALL / "provision.py", "roles.py": INSTALL / "roles.py",
           "runtime-operations-v1.json": NEXT / "contracts" / "runtime" / "runtime-operations-v1.json"}


def _stage():
    root = pathlib.Path(tempfile.mkdtemp(prefix="cortex-ship-stage-"))
    atexit.register(shutil.rmtree, root, True)
    package = root / "cortex_ship" / "install"
    package.mkdir(parents=True)
    (root / "cortex_ship" / "__init__.py").write_bytes(b"")
    (package / "__init__.py").write_bytes(b"")
    for name, source in SOURCES.items():
        raw = source.read_bytes()
        (package / name).write_bytes(raw)
        assert hashlib.sha256((package / name).read_bytes()).digest() == hashlib.sha256(raw).digest()
    return root


STAGE = _stage()
sys.path.insert(0, str(NEXT / "src"))
sys.path.insert(0, str(STAGE))

from cortex_core.runtime import custody, resources  # noqa: E402
from cortex_core.runtime.tests.test_installer_composition import (  # noqa: E402
    NonsecretSQLFixture, SyntheticTransactionStatus)
from cortex_core.runtime.tests.test_installer_custody import PURPOSES, SourceAdapters  # noqa: E402
from cortex_core.schema import seed  # noqa: E402
from cortex_ship.install import roles  # noqa: E402

from _lifecycle_doubles import EngineDouble  # noqa: E402
from test_render import base_config  # noqa: E402

UNLOCK = 99  # SourceAdapters' sentinel: never opened or read
KEY_ID = "NONSECRET-id"
OPERATION = "NONSECRET-operation"
DEADLINE = 200.0  # in the shared clock double's domain (it starts at 100.0)


def password(purpose):
    return ("NONSECRET-bound-" + purpose + "-fixture-long").encode()


def install_config(vault):
    """test_render's config with the one install login the actual catalog requires."""
    value = base_config(vault)
    value["roles"]["installer_enrollment"]["login"] = roles.INSTALLER_LOGIN
    return value


@contextmanager
def composed(render, config):
    """Finn's composition doubles exactly as his setUp, with render on the same clock."""
    case = unittest.TestCase()
    case.config, case.password = config, password
    adapters = SourceAdapters()
    with ExitStack() as stack:
        stack.enter_context(adapters.installed())
        fixture = NonsecretSQLFixture(case)
        clock = SimpleNamespace(monotonic=lambda: adapters.clock)
        stack.enter_context(patch.object(resources, "time", clock))
        stack.enter_context(patch.object(render, "time", clock))
        stack.enter_context(patch.object(resources, "_certificate",
                                         return_value=(pathlib.Path("/NONSECRET/ca"), b"NONSECRET-ca-digest")))
        stack.enter_context(patch.object(resources, "_driver_connect", lambda **kwargs: fixture.connect(**kwargs)))
        pq = ModuleType("psycopg.pq")
        pq.TransactionStatus = SyntheticTransactionStatus
        stack.enter_context(patch.dict(sys.modules, {"psycopg.pq": pq}))
        stack.enter_context(patch.object(seed.secrets, "token_hex", return_value="c" * 64))
        stack.enter_context(patch.object(seed, "uuid4", side_effect=[
            UUID("10000000-0000-0000-0000-000000000004"), UUID("10000000-0000-0000-0000-000000000005")]))
        yield SimpleNamespace(adapters=adapters, fixture=fixture)


@contextmanager
def traced(log, names):
    """Appends ("call"|"return", function, file, locals|value) for the named functions to log."""
    previous = sys.getprofile()

    def profile(frame, event, arg):
        code = frame.f_code
        key = (code.co_name, pathlib.Path(code.co_filename).name)
        if key in names and event in ("call", "return"):
            log.append((event, *key, arg if event == "return" else dict(frame.f_locals)))
    sys.setprofile(profile)
    try:
        yield log
    finally:
        sys.setprofile(previous)


class BootstrapEngine(EngineDouble):
    """EngineDouble plus the one foreground run and volume lookup; argv and input only."""

    def __init__(self, log, *, run_exit=0, volumes=(), **kwargs):
        super().__init__(log, **kwargs)
        self.run_exit, self.volumes, self.inputs = run_exit, set(volumes), []

    def lookup(self, kind, name):
        if kind == "volume":
            return name if name in self.volumes else None
        return super().lookup(kind, name)

    def run(self, argv, *, input_fd, deadline):
        name = argv[argv.index("--name") + 1]
        data = os.read(input_fd, 8192) if input_fd is not None else None
        self.inputs.append((name, data))
        self.log.append(("run", name, tuple(argv), data is not None))
        if self.run_exit == 0:
            self.volumes.update(arg.split(":", 1)[0] for arg, prev in zip(argv[1:], argv)
                                if prev == "-v" and not arg.startswith("/"))
        return self.run_exit
