"""Cortex v2 Context & worker interface module.

Owns budgeted context preparation, capability discovery with usage cards,
deterministic harness mirrors, compatibility profiles and the one versioned
operation registry from which HTTP, CLI and MCP presentations are generated.
Domain code here never imports FastAPI, MCP or CLI formatting; transports live
in ``cortex_v2.cli``, ``cortex_v2.mcp`` and ``cortex_v2.clients``.
"""
