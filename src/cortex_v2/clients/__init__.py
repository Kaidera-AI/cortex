"""Python v2 client library, host adapters and consumer compatibility code.

Speaks only the v2 profile: v2 bearer principal, ``X-Cortex-Scope``,
optional ``X-Cortex-Read-Scopes`` and ``Idempotency-Key`` on writes.
Credentials come from an explicit per-installation file/env; shared
administrator credentials and legacy ``ctx1``/``X-Project`` behavior are
rejected, never emulated or silently used as fallback.
"""
