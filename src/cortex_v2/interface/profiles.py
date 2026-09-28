"""Versioned compatibility profiles and consumer adapter contracts (R21/R28,
F12, plan W2 items 6-8).

The legacy profile is an explicit *description* of legacy behavior (ctx1
bearer, ``X-Project``/``X-Agent-Name``, legacy routes, legacy private owner
channel). The v2 API does not emulate it and never infers one profile from
the other. The KOS and OpenKai adapter contracts bound what each consumer may
rely on.
"""

from __future__ import annotations

from typing import Any

PROFILE_VERSION = "cortex.compatibility.v1"

LEGACY_PROFILE: dict[str, Any] = {
    "profile": "legacy-ctx1",
    "authentication": "ctx1 bearer",
    "identity_headers": ["X-Project", "X-Agent-Name"],
    "routes": "legacy unversioned routes",
    "owner_channel": "legacy private owner channel",
    "idempotency": "none",
    "status": "documented-only; the v2 API does not emulate this profile",
    "boundary": (
        "Legacy tokens, project/agent-name headers and legacy routes remain "
        "legacy-only behavior served by old production Cortex. A v2 endpoint "
        "never treats them as v2 authority and never falls back to a legacy "
        "administrator path or shared administrator credential."
    ),
}

V2_PROFILE: dict[str, Any] = {
    "profile": "v2",
    "authentication": "v2 bearer principal",
    "identity_headers": [],
    "scope_header": "X-Cortex-Scope",
    "read_scopes_header": "X-Cortex-Read-Scopes",
    "idempotency": "Idempotency-Key required on writes",
    "operations": "generated from one versioned operation registry",
    "registry_version": "cortex.operations.v2-w2.1",
    "credentials": (
        "Per-installation principal credentials from an explicit file or "
        "environment; shared administrator credentials are rejected."
    ),
    "error_contract": {
        "body": {"error": {"code": "", "message": "", "retryable": False},
                 "request_id": ""},
        "replay_header": "Idempotent-Replay",
    },
}

KOS_ADAPTER_CONTRACT: dict[str, Any] = {
    "consumer": "KOS (scheduler, worktrees, return outbox, console, Beat)",
    "kos_app_database": "not-accessed",
    "feed_resync": "required",
    "mapped_surfaces": ["scheduler", "worktree", "return-outbox"],
    "transport": "v2 profile over the versioned operation registry",
    "guarantees": [
        "KOS owns scheduling, worktrees, host execution and its app DB; "
        "Cortex never queries or migrates it.",
        "An uncertain completion retries the return command with the same "
        "idempotency key, never the original side effect.",
        "An expired or missing feed cursor yields resync_required plus the "
        "snapshot route, not silent loss.",
        "Duplicate spawns still resolve to one claim winner through the "
        "coordination module's claim operations.",
    ],
}

OPENKAI_ADAPTER_CONTRACT: dict[str, Any] = {
    "consumer": "OpenKai (with or without KOS)",
    "memory_backend": ["off", "cortex"],
    "operations": ["recall", "record", "learn"],
    "transcript_ingest": "opt-in",
    "omp_fallback": "forbidden",
    "omp_import": "not-authorized",
    "transport": "v2 profile over the versioned operation registry",
    "guarantees": [
        "memory.backend=cortex replaces the legacy OMP memory pipeline; it is "
        "never a second pipeline alongside it.",
        "backend=off disables recall/record/learn with an explicit disabled "
        "result; nothing is silently buffered elsewhere.",
        "An unreachable or erroring Cortex yields an honest unavailable "
        "result; there is no OMP fallback path in the adapter.",
        "Legacy OMP/Hindsight/Mnemopi files and rows are preserved but not "
        "imported; the current architecture authorizes no importer.",
        "Transcript ingestion happens only with explicit opt-in consent.",
    ],
}

WORKER_CLIENT_PROFILES: dict[str, Any] = {
    "description": (
        "Every Kaidera AI worker connects with its own independently "
        "installed, scoped client/principal profile under the v2 profile."
    ),
    "consumers": {
        "kos-cli": "v2",
        "beat": "v2",
        "console": "v2",
        "mcp": "v2",
        "kos": "v2",
        "kos-infra": "v2",
        "connect": "v2",
        "kaidera": "v2",
        "openkai": "v2",
        "second-brain": (
            "explicit integration hold: its source, exact client surface and "
            "installation identity are required inputs; never reuse another "
            "worker's credential"
        ),
    },
}


def compatibility_profiles() -> dict[str, Any]:
    from .registry import REGISTRY_VERSION

    return {
        "profile_version": PROFILE_VERSION,
        "legacy": LEGACY_PROFILE,
        "v2": {**V2_PROFILE, "registry_version": REGISTRY_VERSION},
        "adapters": {
            "kos": KOS_ADAPTER_CONTRACT,
            "openkai": OPENKAI_ADAPTER_CONTRACT,
        },
        "worker_clients": WORKER_CLIENT_PROFILES,
    }
