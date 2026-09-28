"""Cortex v2 Verification module (R19): evidence/claim verification with
explicit UNVERIFIABLE verdicts, source/revision citations, readback
evidence and independence from the producing principal. No result
laundering: verification never turns an agent's assertion into truth.

``latest_for_receipt`` is the published read model consumed by the
coordination work-product receipt view.
"""

from __future__ import annotations

from .operations import OPERATIONS
from .records import (
    get_verification,
    latest_for_receipt,
    list_for_subject,
    record_verification,
)

__all__ = [
    "OPERATIONS",
    "get_verification",
    "latest_for_receipt",
    "list_for_subject",
    "record_verification",
]
