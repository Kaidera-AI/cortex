"""Run first-install control as a private, host-Podman-only Unix service."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import uvicorn

from .config import PRODUCTION_INSTANCE, active_profile

SOCKET_DIRECTORY = Path("/tmp/cortex-control")
SOCKET_PATH = SOCKET_DIRECTORY / "bootstrap.sock"


def main() -> None:
    if active_profile().instance_id != PRODUCTION_INSTANCE:
        raise RuntimeError("bootstrap control is available only for production installs")
    SOCKET_DIRECTORY.mkdir(mode=0o700)
    directory = SOCKET_DIRECTORY.stat()
    if (
        directory.st_uid != os.geteuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise RuntimeError("bootstrap socket directory is not private")
    old_umask = os.umask(0o077)
    try:
        with socket.socket(socket.AF_UNIX) as endpoint:
            endpoint.bind(str(SOCKET_PATH))
            os.chmod(SOCKET_PATH, 0o600)
            endpoint.listen(128)
            uvicorn.run(
                "cortex_v2.app:create_bootstrap_app",
                factory=True,
                fd=endpoint.fileno(),
                access_log=False,
            )
    finally:
        SOCKET_PATH.unlink(missing_ok=True)
        os.umask(old_umask)


if __name__ == "__main__":
    main()
