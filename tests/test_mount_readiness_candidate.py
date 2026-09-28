from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from cortex_v2.config import KAI_TEST_INSTANCE, active_profile

PROFILE = active_profile()
if PROFILE.instance_id != KAI_TEST_INSTANCE:
    pytest.skip(
        "mount readiness suite requires the integrated instance",
        allow_module_level=True,
    )

SECRETS = Path(os.environ["CORTEX_V2_SANDBOX_SECRETS_DIR"])
API_URL = os.environ["CORTEX_V2_TEST_API_URL"]
FIXTURE = json.loads((SECRETS / PROFILE.secret_name("fixture")).read_text())
WORKER_TOKEN = (SECRETS / PROFILE.secret_name("worker-token")).read_text().strip()
PROJECT_ALIAS = FIXTURE["project_alias"]

EXPECTED_READY_MODULES = (
    "identity_memory",
    "interface",
    "ingest",
    "coordination",
    "processing",
    "retrieval",
    "feed",
    "verification",
    "ops",
)


class _RedactedAuthorization(str):
    def __repr__(self) -> str:
        return "'Bearer [REDACTED]'"


def test_capabilities_lists_w1_and_all_mounted_modules_ready() -> None:
    with httpx.Client(base_url=API_URL, timeout=15) as client:
        unauthenticated = client.get("/v1/capabilities")
        response = client.get(
            "/v1/capabilities",
            headers={
                "Authorization": _RedactedAuthorization(f"Bearer {WORKER_TOKEN}"),
                "X-Cortex-Scope": PROJECT_ALIAS,
            },
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    data = response.json()["data"]
    modules = {entry["module"]: entry for entry in data["modules"]}
    for name in EXPECTED_READY_MODULES:
        assert name in modules, name
        assert modules[name]["state"] == "ready", (name, modules[name])

    w1_operations = set(modules["identity_memory"]["operations"])
    assert {
        "memory.record",
        "memory.inspect",
        "content.create",
        "auth.enroll_principal",
    } <= w1_operations
    assert "capability.discover" in set(modules["interface"]["operations"])
    assert "coordination.handoff.create" in set(
        modules["coordination"]["operations"]
    )
    assert {"ops.doctor", "ops.backup_create"} <= set(modules["ops"]["operations"])
