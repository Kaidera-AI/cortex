"""cortex2 CLI entrypoint: registry-generated subcommands over the client."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, TextIO

from ..clients.client import CortexClient
from ..clients.config import load_client_profile
from ..clients.errors import (
    ClientConfigError,
    ClientError,
    CortexApiError,
    CortexTransportError,
    IdempotencyKeyRequired,
    ScopeRequired,
)
from ..interface.discovery import render_usage_card
from ..interface.registry import OperationRegistry, build_registry
from ..interface.roles import activated_media_roles, resolve_worker_roles

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_API = 3
EXIT_TRANSPORT = 4

ISSUING_OPERATIONS = frozenset({
    "auth.enroll_principal", "auth.rotate_credential",
    "auth.recover_owner", "projects.create",
})


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            isinstance(key, str) and (key == "token" or key.endswith("_token"))
            or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _emit(stream: TextIO, payload: Any) -> None:
    stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _emit_text(stream: TextIO, text: str) -> None:
    stream.write(text if text.endswith("\n") else text + "\n")


def build_parser(registry: OperationRegistry) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex2",
        description=(
            "Cortex v2 command line client. Every subcommand is generated "
            f"from the versioned operation registry ({registry.version}, "
            f"digest {registry.digest[:12]}…) and calls the same operation "
            "the HTTP and MCP transports call."
        ),
    )
    parser.add_argument(
        "--config", default=None,
        help="non-secret v2 connection profile; no bearer or token file",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    card = subparsers.add_parser(
        "card", help="print one operation's usage card from the local "
        "registry (works offline)"
    )
    card.add_argument("operation_id")

    capabilities = subparsers.add_parser(
        "capabilities", help="authenticated capability discovery "
        "(capability.discover)"
    )
    capabilities.add_argument("--scope", default=None,
                              help="X-Cortex-Scope alias")
    subparsers.add_parser(
        "protocol", help="unauthenticated protocol descriptor "
        "(protocol.descriptor)"
    )

    for operation in registry.operations_sorted():
        sub = subparsers.add_parser(
            operation.operation_id, help=operation.summary
        )
        sub.add_argument("--data", default=None,
                         help="JSON request payload")
        sub.add_argument("--data-file", default=None,
                         help="read the JSON request payload from a file "
                              "('-' for stdin)")
        sub.add_argument("--scope", default=None, help="X-Cortex-Scope alias")
        sub.add_argument("--read-scopes", default=None,
                         help="comma-separated X-Cortex-Read-Scopes aliases")
        sub.add_argument("--idempotency-key", default=None,
                         help="Idempotency-Key (mandatory for writes)")
        sub.add_argument("--path-param", action="append", default=[],
                         metavar="NAME=VALUE",
                         help="path parameter (repeatable)")
        sub.add_argument("--query", action="append", default=[],
                         metavar="NAME=VALUE",
                         help="query parameter for GET operations "
                              "(repeatable)")
    return parser


def _load_payload(namespace: argparse.Namespace) -> dict[str, Any] | None:
    if namespace.data is not None and namespace.data_file is not None:
        raise ValueError("use either --data or --data-file, not both")
    raw = namespace.data
    if namespace.data_file is not None:
        if namespace.data_file == "-":
            raw = sys.stdin.read()
        else:
            with open(namespace.data_file, encoding="utf-8") as handle:
                raw = handle.read()
    if raw is None:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("the request payload must be a JSON object")
    return parsed


def _pairs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, rest = value.partition("=")
        if not separator or not name:
            raise ValueError("expected NAME=VALUE")
        result[name] = rest
    return result


def _make_client(config: str | None) -> CortexClient:
    profile = load_client_profile(config)
    return CortexClient(profile)


def main(
    argv: list[str] | None = None,
    *,
    client: CortexClient | None = None,
    registry: OperationRegistry | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    registry = registry or build_registry()
    parser = build_parser(registry)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            namespace = parser.parse_args(argv)
    except SystemExit as exc:  # --help exits 0; argparse errors exit 2
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    if not namespace.command:
        parser.print_help(out)
        return EXIT_USAGE

    if namespace.command == "card":
        try:
            operation = registry.get(namespace.operation_id)
        except KeyError:
            _emit_text(
                err,
                f"unknown operation {namespace.operation_id!r}; run "
                "'cortex2 --help' or 'cortex2 capabilities'",
            )
            return EXIT_USAGE
        module = registry.module_state(operation.module)
        card = render_usage_card(
            operation,
            module.state,
            None if module.state == "ready" else module.reason,
            worker_roles=resolve_worker_roles(
                registry, activated=activated_media_roles()
            ),
        )
        _emit(out, card)
        return EXIT_OK

    try:
        if namespace.command == "capabilities":
            active = client or _make_client(namespace.config)
            result = active.call(
                "capability.discover", scope=namespace.scope
            )
            if _contains_secret(result.data):
                _emit_text(err, "server returned a credential; stdout suppressed")
                return EXIT_API
            _emit(out, result.data)
            return EXIT_OK
        if namespace.command == "protocol":
            active = client or _make_client(namespace.config)
            result = active.call("protocol.descriptor")
            if _contains_secret(result.data):
                _emit_text(err, "server returned a credential; stdout suppressed")
                return EXIT_API
            _emit(out, result.data)
            return EXIT_OK
        operation = registry.get(namespace.command)
        if operation.operation_id in ISSUING_OPERATIONS:
            _emit_text(
                err,
                "credential issuance cannot use cortex2 JSON/argv/stdout; "
                "use the approved private enrollment flow",
            )
            return EXIT_USAGE
        payload = _load_payload(namespace)
        if operation.requires_idempotency_key and not (
            namespace.idempotency_key
        ):
            raise IdempotencyKeyRequired(operation.operation_id)
        read_scopes = (
            tuple(
                part.strip()
                for part in namespace.read_scopes.split(",")
                if part.strip()
            )
            if namespace.read_scopes
            else None
        )
        active = client or _make_client(namespace.config)
        result = active.call(
            operation.operation_id,
            payload=payload,
            path_params=_pairs(namespace.path_param),
            scope=namespace.scope,
            read_scopes=read_scopes,
            idempotency_key=namespace.idempotency_key,
            query=_pairs(namespace.query),
        )
        if _contains_secret(result.data):
            _emit_text(
                err,
                "server returned a one-time credential; stdout suppressed. "
                "Ask the lead/owner for safe recovery; replay cannot restore plaintext",
            )
            return EXIT_API
        _emit(
            out,
            {
                "operation_id": result.operation_id,
                "status": result.status,
                "replayed": result.replayed,
                "request_id": result.request_id,
                "data": result.data,
            },
        )
        return EXIT_OK
    except KeyError:
        _emit_text(err, f"unknown command {namespace.command!r}")
        return EXIT_USAGE
    except (ValueError, json.JSONDecodeError) as exc:
        _emit_text(err, f"invalid arguments: {exc}")
        return EXIT_USAGE
    except (IdempotencyKeyRequired, ScopeRequired) as exc:
        _emit_text(err, str(exc))
        return EXIT_USAGE
    except ClientConfigError as exc:
        _emit_text(
            err,
            f"client configuration error: {exc}\nSelect a private key-store identity "
            "with a non-secret connection profile and CORTEX_URL.",
        )
        return EXIT_USAGE
    except CortexApiError as exc:
        _emit(
            err,
            {
                "error": {
                    "code": exc.code,
                    "message": "request denied; consult the error code",
                    "retryable": exc.retryable,
                    "request_id": exc.request_id,
                    "operation_id": exc.operation_id,
                }
            },
        )
        return EXIT_API
    except CortexTransportError:
        _emit_text(err, "transport error: Cortex is unreachable")
        return EXIT_TRANSPORT
    except ClientError as exc:
        _emit_text(err, f"client error: {exc}")
        return EXIT_USAGE
