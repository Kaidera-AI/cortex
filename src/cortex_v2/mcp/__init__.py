"""Cortex v2 MCP server (``python -m cortex_v2.mcp``): stdio and streamable
HTTP.

Tools are generated from the same versioned operation registry as HTTP and
the CLI, and every tool call executes the identical versioned operation
through the v2 client library. MCP adds no direct-complete, heartbeat or
secondary write semantics (R23/F05): writes require the caller's
``idempotency_key`` argument, exactly like the ``Idempotency-Key`` header.

Implemented directly against MCP JSON-RPC (protocol 2025-06-18) with the
stdlib plus FastAPI for streamable HTTP, because the runtime image ships no
MCP SDK; see the module report for the optional dependency request.
"""
