"""Request models for the ingestion connector operations."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, StrictStr, field_validator

from ..models import StrictInput

TranscriptFormat = Literal[
    "claude_jsonl", "codex_jsonl", "beat_jsonl", "generic_jsonl"
]


def _reject_nul(value: str, field: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL character")
    return value


class IngestTranscriptRequest(StrictInput):
    connector_namespace: StrictStr = Field(min_length=1, max_length=64)
    source_key: StrictStr = Field(min_length=1, max_length=200)
    transcript_format: TranscriptFormat
    transcript: StrictStr = Field(min_length=1, max_length=1_000_000)
    source_observed_at: datetime | None = None

    @field_validator("connector_namespace", "source_key")
    @classmethod
    def text_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "text field")

    @field_validator("transcript")
    @classmethod
    def transcript_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "transcript")


class IngestLocalStateRequest(StrictInput):
    connector_namespace: StrictStr = Field(min_length=1, max_length=64)
    source_key: StrictStr = Field(min_length=1, max_length=200)
    capture: dict[str, object]
    captured_at: datetime | None = None

    @field_validator("connector_namespace", "source_key")
    @classmethod
    def text_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "text field")


class DiaryEntry(StrictInput):
    entry_date: date
    text: StrictStr = Field(min_length=1, max_length=65_536)

    @field_validator("text")
    @classmethod
    def text_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "text")


class IngestDiaryRequest(StrictInput):
    connector_namespace: StrictStr = Field(min_length=1, max_length=64)
    source_key: StrictStr = Field(min_length=1, max_length=200)
    entries: list[DiaryEntry] = Field(min_length=1, max_length=512)

    @field_validator("connector_namespace", "source_key")
    @classmethod
    def text_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "text field")


class ChatMessage(StrictInput):
    role: Literal["user", "assistant", "system", "tool"]
    text: StrictStr = Field(min_length=1, max_length=65_536)
    observed_at: datetime | None = None

    @field_validator("text")
    @classmethod
    def text_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "text")


class IngestSaveChatRequest(StrictInput):
    connector_namespace: StrictStr = Field(min_length=1, max_length=64)
    source_key: StrictStr = Field(min_length=1, max_length=200)
    title: StrictStr = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1, max_length=512)

    @field_validator("connector_namespace", "source_key", "title")
    @classmethod
    def text_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "text field")
