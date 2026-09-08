#!/usr/bin/env python3
"""Read-only host adapter over the existing canonical OpenKai provider service.

ProviderConfigService owns parsing, eligibility and derived-projection writes.
This adapter adds no credential writer and exposes only authenticated masked
status. The API mounts the derived cache read-only, never the authority/auth store.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def private_directory(path: Path) -> None:
    """Validation only: do not chmod the shared authority or any ancestor."""
    if not path.is_absolute():
        raise ValueError("private directory must be absolute")
    candidate = Path(path.anchor)
    for part in path.parts[1:]:
        candidate /= part
        metadata = candidate.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
            raise ValueError("unsafe private-directory ancestry")
    metadata = path.lstat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("private directory must be owner-only mode 0700")


def private_bytes(path: Path, limit: int = 256 * 1024) -> bytes:
    private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > limit):
            raise ValueError("unsafe private file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError("private file exceeds size limit")
        return data
    finally:
        os.close(fd)


def service_token(path: Path, *, create: bool = False) -> bytes:
    private_directory(path.parent)
    if create:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secrets.token_urlsafe(48).encode("ascii") + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                # Preserve an interrupted create for explicit inspection.
                raise
    value = private_bytes(path, 1024).strip()
    if not re.fullmatch(rb"[A-Za-z0-9_-]{48,256}", value):
        raise ValueError("invalid provider status token")
    return value


def authority_module():
    """Import unchanged canonical files, or their byte-identical projection."""
    here = Path(__file__).resolve().parent
    directory = here / "provider_authority"
    if not (directory / "openkai_provider_config.py").is_file():
        directory = here.parent / "console" / "app"
    if not (directory / "openkai_provider_config.py").is_file():
        raise ValueError("canonical provider authority implementation is unavailable")
    name = "_cortex_provider_authority_" + hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(directory)]
        package.__package__ = name
        sys.modules[name] = package
    qualified = name + ".openkai_provider_config"
    if qualified not in sys.modules:
        spec = importlib.util.spec_from_file_location(qualified, directory / "openkai_provider_config.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
    return sys.modules[qualified]


def default_registry() -> Path:
    here = Path(__file__).resolve().parent
    projected = here / "provider_registry" / "openkai-providers.json"
    if projected.is_file():
        return projected
    return here.parents[1] / "redistributable" / "config" / "openkai-providers.json"


class ProviderStatus:
    def __init__(self, env_file: Path, registry_file: Path | None = None):
        self.env_file = Path(env_file)
        self.registry_file = Path(registry_file or default_registry())
        if not self.registry_file.is_absolute():
            raise ValueError("registry path must be absolute")
        private_bytes(self.env_file)
        module = authority_module()
        # The canonical registry validates both its pinned JSON and its sibling
        # pin file through this supported environment override at construction.
        previous = os.environ.get("OPENKAI_PROVIDER_REGISTRY_FILE")
        os.environ["OPENKAI_PROVIDER_REGISTRY_FILE"] = str(self.registry_file)
        try:
            self.service = module.ProviderConfigService(env_path=self.env_file,
                                                        registry_path=self.registry_file)
        finally:
            if previous is None:
                os.environ.pop("OPENKAI_PROVIDER_REGISTRY_FILE", None)
            else:
                os.environ["OPENKAI_PROVIDER_REGISTRY_FILE"] = previous

    def status(self) -> dict:
        # Existing shared bytes and mode win. Reject unsafe input before the
        # canonical service's optional mode-repair paths can run.
        private_bytes(self.env_file)
        result = self.service.status()
        # Expose the established masked contract, not resolved_environment or
        # any mutator. The API brackets its projection read with this revision.
        return result

    def prepare(self, token_file: Path) -> dict:
        status = self.status()
        service_token(token_file, create=True)
        return {
            "schema": "cortex.provider-service.v1",
            "projection_dir": str(self.service.projection_dir),
            "registry_file": str(self.registry_file),
            "token_file": str(token_file),
            "projection_revision": status["projection_revision"],
        }


class ProviderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, provider: ProviderStatus, token_file: Path):
        self.provider = provider
        self.token_file = token_file
        super().__init__(address, ProviderHandler)

    def handle_error(self, request, client_address):
        # Request exceptions must not print inputs or provider values.
        return


class ProviderHandler(BaseHTTPRequestHandler):
    server_version = "CortexProviderStatus"
    sys_version = ""

    def log_message(self, *args):
        return

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status: int, value: dict):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            token = service_token(self.server.token_file)
        except Exception:
            self.reply(503, {"detail": "provider_status_unavailable"})
            return
        authorization = self.headers.get_all("Authorization") or []
        if len(authorization) != 1 or not hmac.compare_digest(
                authorization[0].encode("utf-8"), b"Bearer " + token):
            self.reply(401, {"detail": "unauthorized"})
            return
        if self.path != "/provider-config":
            self.reply(404, {"detail": "not_found"})
            return
        try:
            self.reply(200, self.server.provider.status())
        except Exception:
            self.reply(503, {"detail": "provider_authority_unavailable"})

    def mutation_refused(self):
        self.reply(405, {"detail": "read_only_provider_status"})

    do_POST = do_PATCH = do_PUT = do_DELETE = mutation_refused


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "serve"])
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--registry-file", type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--listen", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0"])
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024-65535")
    try:
        provider = ProviderStatus(args.env_file, args.registry_file)
        if args.action == "prepare":
            print(json.dumps(provider.prepare(args.token_file), sort_keys=True))
            return 0
        service_token(args.token_file)
        def stop_requested(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop_requested)
        with ProviderHTTPServer((args.listen, args.port), provider, args.token_file) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Do not surface exception values from authority or credential handling.
        print("provider service refused: private authority, registry or token is unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
