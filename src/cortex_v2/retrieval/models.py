"""Typed retrieval request models (pydantic, extra='forbid' via StrictInput).

Request shapes follow the worker-API contract: intent-oriented search,
explicit graph kind, bounded hops/limits/deadlines and commit-anchored code
queries. Free-form text is untrusted input, never an instruction.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ..models import ContentClass, ScopeAlias, StrictInput

SearchIntent = Literal[
    "known-item", "symbol", "concept", "relationship", "work-history"
]
GraphDirection = Literal["out", "in", "both"]
CodeEdgeKind = Literal["calls", "imports", "contains"]
RetractionReason = Literal["source_invalidated", "source_superseded", "source_deleted"]

RELATION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
REPOSITORY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def _reject_nul(value: str, field: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL character")
    return value


class _ReadScopesMixin(StrictInput):
    read_scopes: list[ScopeAlias] | None = Field(
        default=None, min_length=1, max_length=8
    )

    @field_validator("read_scopes")
    @classmethod
    def scopes_are_database_text(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and any("\x00" in value for value in values):
            raise ValueError("read scopes must not contain a NUL character")
        return values


class HitCitation(StrictInput):
    scope_id: uuid.UUID | None = None
    content_id: uuid.UUID
    revision: StrictInt = Field(ge=1)
    span_start: StrictInt | None = Field(default=None, ge=0)
    span_end: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def spans_are_ordered_pairs(self) -> "HitCitation":
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("citation spans require both start and end")
        if (
            self.span_start is not None
            and self.span_end is not None
            and self.span_end <= self.span_start
        ):
            raise ValueError("citation span_end must exceed span_start")
        return self


class MemorySearchRequest(_ReadScopesMixin):
    query: StrictStr = Field(min_length=1, max_length=512)
    intent: SearchIntent = "concept"
    content_ids: list[uuid.UUID] | None = Field(
        default=None, min_length=1, max_length=8
    )
    content_classes: list[ContentClass] | None = Field(
        default=None, min_length=1, max_length=9
    )
    include_stale: StrictBool = False
    limit: StrictInt = Field(default=10, ge=1, le=25)
    graph_hops: StrictInt = Field(default=1, ge=0, le=2)
    deadline_ms: StrictInt = Field(default=1500, ge=50, le=10_000)

    @field_validator("query")
    @classmethod
    def query_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "query")


class InspectHitRequest(StrictInput):
    citation: HitCitation
    include_assertions: StrictBool = True


class GraphExploreRequest(_ReadScopesMixin):
    kind: Literal["memory", "code"]
    entity_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=8)
    content_id: uuid.UUID | None = None
    relations: list[StrictStr] | None = Field(default=None, min_length=1, max_length=8)
    include_retracted: StrictBool = False
    repository_key: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    symbol_keys: list[StrictStr] | None = Field(
        default=None, min_length=1, max_length=16
    )
    edge_kinds: list[CodeEdgeKind] | None = Field(
        default=None, min_length=1, max_length=3
    )
    head_commit: StrictStr | None = None
    direction: GraphDirection = "both"
    hops: StrictInt = Field(default=1, ge=1, le=3)
    limit: StrictInt = Field(default=50, ge=1, le=200)

    @field_validator("relations")
    @classmethod
    def relations_are_typed(cls, values: list[str] | None) -> list[str] | None:
        if values is not None:
            for value in values:
                if not RELATION_PATTERN.match(value):
                    raise ValueError("relations must match ^[a-z][a-z0-9_]{0,63}$")
        return values

    @field_validator("head_commit")
    @classmethod
    def head_commit_is_hex(cls, value: str | None) -> str | None:
        if value is not None and not COMMIT_PATTERN.match(value):
            raise ValueError("head_commit must be lowercase hex, 7-64 characters")
        return value

    @model_validator(mode="after")
    def seeds_match_kind(self) -> "GraphExploreRequest":
        if self.kind == "memory":
            if not self.entity_ids and self.content_id is None:
                raise ValueError("memory exploration requires entity_ids or content_id")
            if self.repository_key is not None or self.symbol_keys is not None:
                raise ValueError("code selectors are not valid for memory exploration")
        else:
            if not self.repository_key or not self.symbol_keys:
                raise ValueError(
                    "code exploration requires repository_key and symbol_keys"
                )
            if self.entity_ids is not None or self.content_id is not None:
                raise ValueError(
                    "memory selectors are not valid for code exploration"
                )
        return self


class GraphExtractRequest(StrictInput):
    content_id: uuid.UUID
    revision: StrictInt | None = Field(default=None, ge=1, le=2_147_483_647)


class GraphRetractRequest(StrictInput):
    content_id: uuid.UUID
    reason: RetractionReason


class RebuildProjectionsRequest(StrictInput):
    """Strict empty body for memory.rebuild-projections.

    A scoped_write operation always carries a request model so the generic
    mount parses and validates one typed body shape; ``{}`` is the only
    accepted payload and any extra field is rejected.
    """


class CodeFileInput(StrictInput):
    path: StrictStr = Field(min_length=1, max_length=512)
    source: StrictStr = Field(max_length=262_144)

    @field_validator("path")
    @classmethod
    def path_is_relative_and_clean(cls, value: str) -> str:
        _reject_nul(value, "path")
        if value.startswith("/") or value.startswith("\\"):
            raise ValueError("file paths must be repository-relative")
        segments = value.replace("\\", "/").split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise ValueError("file paths must not traverse directories")
        return value

    @field_validator("source")
    @classmethod
    def source_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "source")


class CodePublishIndexRequest(StrictInput):
    repository_key: StrictStr = Field(min_length=1, max_length=128)
    commit_sha: StrictStr
    files: list[CodeFileInput] = Field(min_length=1, max_length=64)

    @field_validator("repository_key")
    @classmethod
    def repository_key_is_typed(cls, value: str) -> str:
        _reject_nul(value, "repository_key")
        if not REPOSITORY_KEY_PATTERN.match(value):
            raise ValueError(
                "repository_key must match ^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
            )
        return value

    @field_validator("commit_sha")
    @classmethod
    def commit_is_hex(cls, value: str) -> str:
        if not COMMIT_PATTERN.match(value):
            raise ValueError("commit_sha must be lowercase hex, 7-64 characters")
        return value


class CodeAnnotateRequest(StrictInput):
    repository_key: StrictStr = Field(min_length=1, max_length=128)
    symbol_key: StrictStr = Field(min_length=1, max_length=512)
    annotation: StrictStr = Field(min_length=1, max_length=2048)

    @field_validator("repository_key", "symbol_key", "annotation")
    @classmethod
    def fields_are_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "field")


class CodeCallersRequest(_ReadScopesMixin):
    repository_key: StrictStr = Field(min_length=1, max_length=128)
    symbol_key: StrictStr = Field(min_length=1, max_length=512)
    head_commit: StrictStr | None = None
    limit: StrictInt = Field(default=50, ge=1, le=200)

    @field_validator("head_commit")
    @classmethod
    def head_commit_is_hex(cls, value: str | None) -> str | None:
        if value is not None and not COMMIT_PATTERN.match(value):
            raise ValueError("head_commit must be lowercase hex, 7-64 characters")
        return value


class CodeImpactRequest(_ReadScopesMixin):
    repository_key: StrictStr = Field(min_length=1, max_length=128)
    changed_files: list[StrictStr] | None = Field(
        default=None, min_length=1, max_length=64
    )
    changed_symbols: list[StrictStr] | None = Field(
        default=None, min_length=1, max_length=64
    )
    head_commit: StrictStr | None = None
    max_hops: StrictInt = Field(default=2, ge=1, le=4)
    limit: StrictInt = Field(default=100, ge=1, le=500)

    @field_validator("head_commit")
    @classmethod
    def head_commit_is_hex(cls, value: str | None) -> str | None:
        if value is not None and not COMMIT_PATTERN.match(value):
            raise ValueError("head_commit must be lowercase hex, 7-64 characters")
        return value

    @model_validator(mode="after")
    def change_set_is_present(self) -> "CodeImpactRequest":
        if not self.changed_files and not self.changed_symbols:
            raise ValueError("provide changed_files, changed_symbols or both")
        return self


class CodeBlastRadiusRequest(CodeImpactRequest):
    pass


class CodeHotspotsRequest(_ReadScopesMixin):
    repository_key: StrictStr = Field(min_length=1, max_length=128)
    head_commit: StrictStr | None = None
    limit: StrictInt = Field(default=25, ge=1, le=100)

    @field_validator("head_commit")
    @classmethod
    def head_commit_is_hex(cls, value: str | None) -> str | None:
        if value is not None and not COMMIT_PATTERN.match(value):
            raise ValueError("head_commit must be lowercase hex, 7-64 characters")
        return value


class CodeAssessChangeRequest(_ReadScopesMixin):
    repository_key: StrictStr = Field(min_length=1, max_length=128)
    base_commit: StrictStr
    target_commit: StrictStr | None = None
    changed_files: list[StrictStr] | None = Field(
        default=None, min_length=1, max_length=64
    )
    changed_symbols: list[StrictStr] | None = Field(
        default=None, min_length=1, max_length=64
    )
    max_hops: StrictInt = Field(default=2, ge=1, le=4)
    limit: StrictInt = Field(default=100, ge=1, le=500)
    deadline_ms: StrictInt = Field(default=2000, ge=50, le=10_000)

    @field_validator("base_commit", "target_commit")
    @classmethod
    def commits_are_hex(cls, value: str | None) -> str | None:
        if value is not None and not COMMIT_PATTERN.match(value):
            raise ValueError("commits must be lowercase hex, 7-64 characters")
        return value

    @model_validator(mode="after")
    def change_is_anchored(self) -> "CodeAssessChangeRequest":
        if (
            not self.target_commit
            and not self.changed_files
            and not self.changed_symbols
        ):
            raise ValueError(
                "provide target_commit, changed_files or changed_symbols"
            )
        return self
