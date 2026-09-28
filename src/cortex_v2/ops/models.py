from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr

from ..models import StrictInput

RetentionClass = Literal[
    "*",
    "decision",
    "lesson",
    "knowledge",
    "progress",
    "diary",
    "message",
    "session",
    "artifact",
    "work_product",
]
RepairKind = Literal["retention_restore"]


class RetentionEnactRequest(StrictInput):
    content_class: RetentionClass
    min_age_days: StrictInt = Field(ge=0, le=36_500)
    action: Literal["archive", "retain"]
    expected_revision: StrictInt = Field(ge=0)


class RetentionArchiveRequest(StrictInput):
    content_class: RetentionClass | None = None
    dry_run: StrictBool = False


class RepairRequest(StrictInput):
    repair_kind: RepairKind
    target_reference: StrictStr = Field(min_length=1, max_length=256)
