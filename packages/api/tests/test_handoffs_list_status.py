"""Fail-loud validation for the GET /handoffs status filter.

The endpoint used to do an exact `status = $2` match with a silent `pending` default,
so a typo'd status returned an empty set with no error — the bug that hid CLAIMED
handoffs from the PM watchdog until it learned to pass status=claimed. The status is
now validated (400 on an unknown value) and `all` is an explicit no-filter escape hatch.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException


@pytest.fixture
def api_module():
    src = Path(__file__).resolve().parent.parent / "main.py"
    spec = importlib.util.spec_from_file_location("cortex_api_handoffs_under_test", src)
    assert spec and spec.loader, f"could not load spec for {src}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_list_handoffs_rejects_invalid_status(api_module):
    """A typo'd status must 400 (fail-loud) — not silently return an empty set."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_module.list_handoffs(status="claimedd"))
    assert exc.value.status_code == 400
    assert "Invalid handoff status" in str(exc.value.detail)


def test_handoff_list_statuses_include_all_and_lifecycle(api_module):
    """The accepted set is the lifecycle + the explicit no-filter escape hatch."""
    assert api_module._HANDOFF_LIST_STATUSES == frozenset(
        {"pending", "claimed", "returned", "completed", "all"}
    )
