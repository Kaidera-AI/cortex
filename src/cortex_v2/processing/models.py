"""Typed request models for the Processing operations.

Same conventions as W1's ``models.py``: strict pydantic types, ``extra="forbid"``,
no whitespace stripping, and NUL characters rejected because PostgreSQL text
cannot store them. Validation happens at the boundary; the domain and the
database re-check what matters.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator

from ..models import StrictInput

Metric = Literal["cosine", "l2", "inner_product"]
Normalization = Literal["l2", "none"]
JobStatus = Literal[
    "queued", "leased", "succeeded", "failed", "cancelled", "quarantined", "blocked"
]
BackfillStage = Literal["auto", "doc.extract", "embed.chunks"]
GenerationSelection = Literal["active", "new"]


def _reject_nul(value: str, field: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL character")
    return value


class ChunkingPolicyInput(StrictInput):
    """Pinned chunking policy identity and parameters."""

    policy_id: StrictStr = Field(min_length=1, max_length=64)
    version: StrictInt = Field(ge=1, le=999_999)
    kind: Literal["structural", "token_window"]
    target_chars: StrictInt = Field(ge=64, le=32_000)
    max_chars: StrictInt = Field(ge=64, le=65_536)
    overlap_chars: StrictInt = Field(ge=0, le=32_000)

    @field_validator("policy_id")
    @classmethod
    def policy_id_is_database_text(cls, value: str) -> str:
        return _reject_nul(value, "policy_id")

    @field_validator("max_chars")
    @classmethod
    def max_chars_covers_target(cls, value: int, info) -> int:
        target = info.data.get("target_chars")
        if isinstance(target, int) and value < target:
            raise ValueError("max_chars must be at least target_chars")
        return value

    @field_validator("overlap_chars")
    @classmethod
    def overlap_is_smaller_than_target(cls, value: int, info) -> int:
        target = info.data.get("target_chars")
        if isinstance(target, int) and value >= target:
            raise ValueError("overlap_chars must be smaller than target_chars")
        return value


class CreateEmbeddingSpaceRequest(StrictInput):
    """Immutable embedding-space semantics (F07).

    ``provider`` names a provider *family* (``hash-local``,
    ``openai-compatible``, ``ollama`` or a deployment-named adapter). A family
    no configured role serves is not rejected here — replaceability matters more
    than a closed list — but the response says whether it is currently known,
    and execution reports a typed ``space_mismatch`` until it is configured.
    """

    space_name: StrictStr = Field(min_length=1, max_length=96)
    provider: StrictStr = Field(min_length=1, max_length=64)
    model_id: StrictStr = Field(min_length=1, max_length=128)
    model_revision: StrictStr | None = Field(default=None, max_length=128)
    dimensions: StrictInt = Field(ge=1, le=16_000)
    normalization: Normalization = "l2"
    metric: Metric = "cosine"
    query_prefix: StrictStr | None = Field(default=None, max_length=256)
    document_prefix: StrictStr | None = Field(default=None, max_length=256)
    chunking: ChunkingPolicyInput

    @field_validator("space_name", "provider", "model_id")
    @classmethod
    def values_are_database_text(cls, value: str) -> str:
        return _reject_nul(value, "space identity")

    def chunking_block(self) -> dict[str, object]:
        return {"schema_version": 1, **self.chunking.model_dump()}


class ActivateGenerationRequest(StrictInput):
    """Switch retrieval routing to a built generation."""

    expected_state: Literal["building", "built", "active"] | None = None


class BackfillRequest(StrictInput):
    """Bounded, resumable selective embedding backfill (R13)."""

    profile_id: uuid.UUID
    space_id: uuid.UUID
    generation: GenerationSelection = "active"
    stage: BackfillStage = "auto"
    then_embed: StrictBool = True
    limit: StrictInt = Field(default=50, ge=1, le=200)
    priority: StrictInt = Field(default=100, ge=0, le=1000)
    cursor: StrictStr | None = Field(default=None, max_length=128)
    reserve_budget: StrictBool = True


class CancelJobRequest(StrictInput):
    reason: StrictStr | None = Field(default=None, max_length=512)

    @field_validator("reason")
    @classmethod
    def reason_is_database_text(cls, value: str | None) -> str | None:
        return None if value is None else _reject_nul(value, "reason")


class ListJobsRequest(StrictInput):
    status: JobStatus | None = None
    job_kind: StrictStr | None = Field(default=None, max_length=64)
    required_role: Literal["core", "doc", "image", "audio", "video"] | None = None
    batch_id: uuid.UUID | None = None
    cursor: StrictStr | None = Field(default=None, max_length=128)
    limit: StrictInt = Field(default=25, ge=1, le=100)
    include_summary: StrictBool = True


class JobStatusRequest(StrictInput):
    include_attempts: StrictBool = True


class CoverageRequest(StrictInput):
    """Coverage is always measured against one profile's intended selection."""

    profile_id: uuid.UUID
    space_id: uuid.UUID | None = None
    generation_id: uuid.UUID | None = None


__all__ = [
    "ActivateGenerationRequest",
    "BackfillRequest",
    "CancelJobRequest",
    "ChunkingPolicyInput",
    "CoverageRequest",
    "CreateEmbeddingSpaceRequest",
    "JobStatusRequest",
    "ListJobsRequest",
]
