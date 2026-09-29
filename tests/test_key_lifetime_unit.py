"""Client and API credential-lifetime decisions share exact UTC boundaries."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from cortex_v2.clients.key_store import KeyMetadata, KeyStoreError


ISSUED_AT = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc)
EXPIRES_AT = ISSUED_AT + dt.timedelta(days=180)


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (dt.timedelta(days=149), None),
        (dt.timedelta(days=150), "due"),
        (dt.timedelta(days=180), "expired"),
        (dt.timedelta(days=180, microseconds=1), "expired"),
    ],
)
def test_issuance_day_boundaries(elapsed: dt.timedelta, expected: str | None) -> None:
    from cortex_v2.key_lifetime import due_state

    now = ISSUED_AT + elapsed
    assert due_state(EXPIRES_AT, now) == expected
    assert KeyMetadata("user", EXPIRES_AT.isoformat()).due_state(now) == expected


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (dt.timedelta(days=30, microseconds=1), None),
        (dt.timedelta(days=30), "due"),
        (dt.timedelta(microseconds=1), "due"),
        (dt.timedelta(0), "expired"),
        (-dt.timedelta(microseconds=1), "expired"),
    ],
)
def test_exact_due_window_edges(remaining: dt.timedelta, expected: str | None) -> None:
    from cortex_v2.key_lifetime import due_state

    expiry = ISSUED_AT + remaining
    assert due_state(expiry, ISSUED_AT) == expected
    assert KeyMetadata("kos", expiry.isoformat()).due_state(ISSUED_AT) == expected


@pytest.mark.parametrize("naive_input", ["expiry", "clock"])
def test_leaf_rejects_naive_datetime(naive_input: str) -> None:
    from cortex_v2.key_lifetime import due_state

    expiry = EXPIRES_AT.replace(tzinfo=None) if naive_input == "expiry" else EXPIRES_AT
    now = ISSUED_AT.replace(tzinfo=None) if naive_input == "clock" else ISSUED_AT
    with pytest.raises(ValueError, match="timezone"):
        due_state(expiry, now)


def test_client_metadata_rejects_naive_expiry_and_clock() -> None:
    with pytest.raises(KeyStoreError, match="timezone"):
        KeyMetadata("user", EXPIRES_AT.replace(tzinfo=None).isoformat()).due_state(ISSUED_AT)
    with pytest.raises(KeyStoreError, match="timezone"):
        KeyMetadata("user", EXPIRES_AT.isoformat()).due_state(
            ISSUED_AT.replace(tzinfo=None)
        )


def test_client_can_import_lifetime_without_server_dependencies() -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from cortex_v2.clients.key_store import KeyMetadata; "
        "from cortex_v2.key_lifetime import due_state; "
        "assert not ({'asyncpg', 'fastapi'} & set(sys.modules)); "
        "assert not ({'cortex_v2.app', 'cortex_v2.store'} & set(sys.modules))"
    )
    imported = subprocess.run(
        [sys.executable, "-S", "-c", script, str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
