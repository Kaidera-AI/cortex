"""``python -m cortex_v2.mcp`` entrypoint: stdio (default) or streamable HTTP.

Stdio mode inherits one installation-bound identity from the explicit
per-installation client profile. HTTP mode authenticates every request from
its own bearer header and never uses an ambient credential.
"""

from __future__ import annotations

import argparse
import sys


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
        help="per-installation v2 client profile JSON (stdio identity / "
        "http base URL)",
    )
    namespace = parser.parse_args(argv)
    from .server import run_http, run_stdio

    if namespace.http:
        run_http(host=namespace.host, port=namespace.port,
                 config=namespace.config)
        return 0
    run_stdio(config=namespace.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
