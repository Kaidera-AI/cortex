"""Request models for Context & worker interface operations.

Same conventions as ``cortex_v2.models``: strict pydantic inputs with
``extra='forbid'``, bounded lengths and NUL rejection for Postgres text.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import Field, StrictInt, StrictStr, field_validator

from ..models import StrictInput

IntentClass = Literal[
    "code_change",
    "debug_symbol",
    "architecture",
    "research",
    "record_or_return",
    "prose_edit",
]

PIN_PATTERN = re.compile(
    r"^(persona|rule:[a-z0-9][a-z0-9._-]{0,95}|skill:[a-z0-9][a-z0-9._-]{0,95})$"
)
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")


def _reject_nul(value: str, field: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL character")
    return value


class ContextPrepareRequest(StrictInput):
    intent: IntentClass
    task_ref: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    task_text: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    repository_ref: StrictStr | None = Field(
        default=None, min_length=1, max_length=256
    )
    change_ref: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    budget_bytes: StrictInt = Field(default=32_768, ge=2_048, le=262_144)
    recall_limit: StrictInt = Field(default=5, ge=0, le=25)

    @field_validator("task_ref", "task_text", "repository_ref", "change_ref")
    @classmethod
    def text_is_postgres_text(cls, value: str | None) -> str | None:
        return None if value is None else _reject_nul(value, "text field")


class PersonaEnactRequest(StrictInput):
    persona_id: uuid.UUID | None = None
    template_version: StrictStr = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    body: StrictStr = Field(min_length=1, max_length=65_536)

    @field_validator("body")
    @classmethod
    def body_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "body")

    @field_validator("template_version")
    @classmethod
    def template_version_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "template_version")


class RuleEnactRequest(StrictInput):
    rule_id: uuid.UUID | None = None
    slug: StrictStr = Field(min_length=1, max_length=96)
    obligation: Literal["mandatory", "optional"]
    body: StrictStr = Field(min_length=1, max_length=65_536)

    @field_validator("slug")
    @classmethod
    def slug_is_valid(cls, value: str) -> str:
        value = _reject_nul(value, "slug")
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9._-]{0,95}$"
            )
        return value

    @field_validator("body")
    @classmethod
    def body_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "body")


class SkillEnactRequest(StrictInput):
    skill_id: uuid.UUID | None = None
    slug: StrictStr = Field(min_length=1, max_length=96)
    when_to_use: StrictStr = Field(min_length=1, max_length=2_048)
    body: StrictStr = Field(min_length=1, max_length=65_536)

    @field_validator("slug")
    @classmethod
    def slug_is_valid(cls, value: str) -> str:
        value = _reject_nul(value, "slug")
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError(
                "slug must match ^[a-z0-9][a-z0-9._-]{0,95}$"
            )
        return value

    @field_validator("when_to_use", "body")
    @classmethod
    def text_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "text field")


class SkillBindingEntry(StrictInput):
    skill_id: uuid.UUID
    revision: StrictInt | None = Field(default=None, ge=1)
    precedence: StrictInt = Field(ge=1, le=1_000)


class SkillBindRequest(StrictInput):
    bindings: list[SkillBindingEntry] = Field(min_length=1, max_length=32)
    expected_revision: StrictInt = Field(ge=0)


class SkillGetQuery(StrictInput):
    revision: int | None = Field(default=None, ge=1)

    @field_validator("revision", mode="before")
    @classmethod
    def coerce_optional_query_revision(cls, value: Any) -> Any:
        # GET query parameters arrive as strings; an omitted or empty
        # optional field means "latest revision", never a 422.
        if value is None or value == "":
            return None
        if isinstance(value, str):
            if not value.isdigit():
                raise ValueError("revision must be a positive integer")
            return int(value)
        return value


class HarnessPins(StrictInput):
    pins: dict[StrictStr, StrictInt] = Field(default_factory=dict, max_length=64)

    @field_validator("pins")
    @classmethod
    def pins_are_valid(cls, value: dict[str, int]) -> dict[str, int]:
        for key, revision in value.items():
            if not PIN_PATTERN.fullmatch(key):
                raise ValueError(
                    f"pin {key!r} must be 'persona', 'rule:<slug>' or "
                    "'skill:<slug>'"
                )
            if revision < 1:
                raise ValueError(f"pin {key!r} revision must be >= 1")
        return value


class HarnessPreviewRequest(HarnessPins):
    mirror_label: StrictStr = Field(min_length=1, max_length=96)

    @field_validator("mirror_label")
    @classmethod
    def label_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "mirror_label")


class HarnessApplyRequest(HarnessPreviewRequest):
    observed_manifest: dict[StrictStr, StrictStr] | None = Field(
        default=None, max_length=512
    )


class HarnessRollbackRequest(HarnessPins):
    mirror_label: StrictStr = Field(min_length=1, max_length=96)
    target_generation: StrictInt | None = Field(default=None, ge=1)
    observed_manifest: dict[StrictStr, StrictStr] | None = Field(
        default=None, max_length=512
    )

    @field_validator("mirror_label")
    @classmethod
    def label_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "mirror_label")


class HarnessDriftRequest(StrictInput):
    mirror_label: StrictStr = Field(min_length=1, max_length=96)
    observed_manifest: dict[StrictStr, StrictStr] = Field(min_length=0, max_length=512)

    @field_validator("mirror_label")
    @classmethod
    def label_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "mirror_label")
