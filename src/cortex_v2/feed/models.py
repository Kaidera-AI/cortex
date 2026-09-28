"""Strict request models for feed operations (R20)."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field, StrictInt, StrictStr

from ..models import StrictInput


def _reject_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text must not contain a NUL character")
    return value


ReasonText = Annotated[
    StrictStr, Field(min_length=1, max_length=512), AfterValidator(_reject_nul)
]


class DispatchFeedRequest(StrictInput):
    batch_limit: StrictInt = Field(default=200, ge=1, le=5_000)


class PruneFeedRequest(StrictInput):
    older_than_days: StrictInt = Field(ge=1, le=3_650)


class AdvanceFeedGenerationRequest(StrictInput):
    reason: ReasonText
