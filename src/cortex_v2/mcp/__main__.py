"""``python -m cortex_v2.mcp`` entrypoint: stdio (default) or streamable HTTP.

Stdio selects one project member and reads its private key per outgoing request.
HTTP authenticates every request with its own bearer; it never loads a
process-wide credential.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..clients.errors import ClientConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cortex_v2.mcp",
        description="Cortex v2 MCP server (tools generated from the one "
        "versioned operation registry)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true",
                      help="line-delimited JSON-RPC over stdio (default)")
    mode.add_argument("--http", action="store_true",
                      help="streamable HTTP JSON-RPC on --host/--port")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8610)
    parser.add_argument(
        "--config", default=None,
        help="non-secret installation URL/identity profile (no bearer fields)",
    )
    parser.add_argument("--installation", help="stdio key-store installation")
    parser.add_argument("--project", help="stdio key-store project")
    parser.add_argument("--name", help="stdio key-store identity name")
    parser.add_argument("--project-root", type=Path, help="stdio physical project root")
    namespace = parser.parse_args(argv)
    if namespace.http and (namespace.installation or namespace.project or namespace.name or namespace.project_root):
        parser.error("HTTP MCP accepts only caller credentials; identity options are stdio-only")
    from .server import run_http, run_stdio

    if namespace.http:
        run_http(host=namespace.host, port=namespace.port,
                 config=namespace.config)
        return 0
    try:
        run_stdio(
            config=namespace.config, installation=namespace.installation,
            project=namespace.project, name=namespace.name,
            project_root=namespace.project_root,
        )
    except ClientConfigError:
        sys.stderr.write("MCP member configuration unavailable; select installation, project, member and physical root.\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
