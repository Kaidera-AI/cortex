"""Shared credential-lifetime boundaries for API and local key metadata."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

DUE_WINDOW = timedelta(days=30)


def due_state(expires_at: datetime, now: datetime) -> Literal["due", "expired"] | None:
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("credential expiry must include a timezone")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("credential status clock must include a timezone")
    if expires_at <= now:
        return "expired"
    if expires_at - now <= DUE_WINDOW:
        return "due"
    return None
