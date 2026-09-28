"""Human commands read local identities without displaying bearer credentials."""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import TextIO

from ..clients.client import CortexClient
from ..clients.config import _connection, load_client_profile
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
    status = commands.add_parser("status", help="show local keys due for renewal or expired")
    status.add_argument("--installation", help="installation key-store identity")
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


def _warn_if_due(client: CortexClient, output: TextIO, now: datetime.datetime | None) -> None:
    profile = client.profile
    if profile.credential_store is None:
        return
    if not profile.credential_project or not profile.credential_name:
        raise ClientConfigError("stored key lacks a selected project and identity")
    metadata = profile.credential_store.metadata(
        profile.credential_project, profile.credential_name,
    )
    if metadata is None:
        raise ClientConfigError("selected local credential metadata is missing")
    clock = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    state = metadata.due_state(clock)
    if state is not None:
        output.write("Warning: " + json.dumps({
            "project": profile.credential_project,
            "name": profile.credential_name,
            "state": state,
            "expires_at": metadata.expires_at,
        }, sort_keys=True) + "\n")


def human_main(
    argv: list[str] | None = None,
    *,
    client: CortexClient | None = None,
    store: KeyStore | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    now: datetime.datetime | None = None,
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
        if args.command == "status":
            selected_store = store
            if selected_store is None:
                installation = args.installation
                if not installation:
                    connection, _ = _connection(args.config)
                    installation = connection.get("installation")
                if not installation:
                    raise ClientConfigError("select --installation or configure an installation")
                selected_store = KeyStore(installation)
            elif args.installation and selected_store.installation != args.installation:
                raise ClientConfigError("selected installation differs from the private key store")
            for key in selected_store.due(now=now):
                out.write(json.dumps({
                    "project": key.project, "name": key.name,
                    "managed_by": key.managed_by, "expires_at": key.expires_at,
                    "state": key.state,
                }, sort_keys=True) + "\n")
            return 0
        if client is None:
            profile = load_client_profile(
                args.config, store=store, installation=args.installation,
                project=args.project, name=args.name,
            )
            client = CortexClient(profile)
        _warn_if_due(client, err, now)
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
