"""Human commands read local identities without displaying bearer credentials."""

from __future__ import annotations

import argparse
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import TextIO

from ..clients.client import CortexClient
from ..clients.config import load_client_profile
from ..clients.errors import ClientConfigError, CortexApiError, CortexTransportError
from ..clients.key_store import KeyStore, KeyStoreError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex", description="Cortex human identity commands")
    parser.add_argument("--config", help="non-secret connection profile (no bearer fields)")
    commands = parser.add_subparsers(dest="command")
    whoami = commands.add_parser("whoami", help="show the current key's verified project rights")
    whoami.add_argument("--installation", help="installation key-store identity")
    whoami.add_argument("--project", help="project whose rights to inspect")
    whoami.add_argument("--name", help="local key name (default: owner)")
    return parser


def _render_identity(data: object, output: TextIO) -> None:
    if not isinstance(data, dict):
        raise ClientConfigError("whoami response lacks authenticated identity")
    rights = data.get("rights")
    required = ("installation_id", "principal_id", "name", "role")
    if (any(not isinstance(data.get(field), str) or not data[field] for field in required)
            or not isinstance(rights, dict)
            or any(type(rights.get(key)) is not bool for key in (
                "read", "write", "manage_keys", "create_projects",
            ))):
        raise ClientConfigError("whoami response lacks verified project rights")
    project = data.get("project")
    if project is not None and not isinstance(project, str):
        raise ClientConfigError("whoami response has an invalid project")
    output.write(f"Installation: {data['installation_id']}\n")
    output.write(f"Project: {project or '(installation)'}\n")
    output.write(f"Identity: {data['name']} ({data['role']})\n")
    output.write(f"Principal ID: {data['principal_id']}\n")
    output.write("Rights: " + ", ".join(
        f"{key}={str(rights[key]).lower()}" for key in (
            "read", "write", "manage_keys", "create_projects",
        )
    ) + "\n")


def human_main(
    argv: list[str] | None = None,
    *,
    client: CortexClient | None = None,
    store: KeyStore | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = _parser()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if args.command is None:
        parser.print_help(out)
        return 2
    try:
        if client is None:
            profile = load_client_profile(
                args.config, store=store, installation=args.installation,
                project=args.project, name=args.name,
            )
            client = CortexClient(profile)
        result = client.call(
            "auth.principal", query={"project": args.project} if args.project else None,
        )
        if args.project and (
            not isinstance(result.data, dict) or result.data.get("project") != args.project
        ):
            raise ClientConfigError("whoami did not verify the selected project")
        if args.name and (
            not isinstance(result.data, dict) or result.data.get("name") != args.name
        ):
            raise ClientConfigError("whoami did not verify the selected identity")
        _render_identity(result.data, out)
        return 0
    except CortexApiError as exc:
        remedies = {
            "invalid_credential": "Ask the project lead/owner to re-enroll this identity.",
            "key_expired": "Ask the project lead/owner to renew this identity.",
            "scope_access_denied": "Ask the project lead/owner for access.",
        }
        err.write(f"Cortex refused whoami: {exc.code} (HTTP {exc.status}).\n")
        if exc.code in remedies:
            err.write(remedies[exc.code] + "\n")
        return 3
    except (ClientConfigError, KeyStoreError) as exc:
        err.write(f"Credential unavailable: {exc}\n")
        return 2
    except CortexTransportError:
        err.write("Cortex is unreachable; no identity was verified.\n")
        return 4


if __name__ == "__main__":
    raise SystemExit(human_main())
