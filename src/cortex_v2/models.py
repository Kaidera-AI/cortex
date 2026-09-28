from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


ScopeAlias = Annotated[StrictStr, Field(min_length=1, max_length=96)]
ActorKind = Literal["human", "agent", "service"]
MembershipRole = Literal["owner", "lead", "member", "observer"]
ContentClass = Literal[
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
ConnectorKind = Literal["session_ingest", "file", "api", "manual"]


def _reject_nul(value: str, field: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL character")
    return value


class CreateMemoryRecord(StrictInput):
    record_type: Literal["decision", "lesson", "knowledge", "note"]
    body: StrictStr = Field(min_length=1, max_length=65_536)

    @field_validator("body")
    @classmethod
    def body_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "body")


class SearchRequest(StrictInput):
    query: StrictStr = Field(min_length=1, max_length=512)
    read_scopes: list[ScopeAlias] = Field(min_length=1, max_length=8)
    limit: StrictInt = Field(default=10, ge=1, le=25)

    @field_validator("query")
    @classmethod
    def query_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "query")

    @field_validator("read_scopes")
    @classmethod
    def scopes_are_database_text(cls, values: list[str]) -> list[str]:
        if any("\x00" in value for value in values):
            raise ValueError("read scopes must not contain a NUL character")
        return values


class ScopeGrantRequest(StrictInput):
    alias: ScopeAlias
    can_read: StrictBool = True
    can_write: StrictBool = False
    can_publish: StrictBool = False


class EnrollPrincipalRequest(StrictInput):
    principal_name: StrictStr = Field(min_length=1, max_length=128)
    actor_kind: ActorKind
    expires_in_seconds: StrictInt | None = Field(default=None, ge=1, le=31_536_000)
    scopes: list[ScopeGrantRequest] = Field(default_factory=list, max_length=8)

    @field_validator("principal_name")
    @classmethod
    def name_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "principal_name")


class RotateCredentialRequest(StrictInput):
    principal_id: uuid.UUID | None = None
    expires_in_seconds: StrictInt | None = Field(default=None, ge=1, le=31_536_000)


class RevokeCredentialRequest(StrictInput):
    credential_id: uuid.UUID


class RevokePrincipalRequest(StrictInput):
    principal_id: uuid.UUID


class BindScopeRequest(StrictInput):
    principal_id: uuid.UUID
    alias: ScopeAlias
    can_read: StrictBool = True
    can_write: StrictBool = False
    can_publish: StrictBool = False


class RevokeGrantRequest(StrictInput):
    principal_id: uuid.UUID
    alias: ScopeAlias


class RecoverOwnerRequest(StrictInput):
    recovery_token: StrictStr = Field(min_length=43, max_length=128)
    expires_in_seconds: StrictInt | None = Field(default=None, ge=1, le=31_536_000)


class RenameScopeRequest(StrictInput):
    new_alias: ScopeAlias

    @field_validator("new_alias")
    @classmethod
    def alias_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "new_alias")


class RosterEntry(StrictInput):
    principal_id: uuid.UUID
    role: MembershipRole
    responsibility: StrictStr | None = Field(default=None, min_length=1, max_length=128)


class EnactRosterRequest(StrictInput):
    entries: list[RosterEntry] = Field(min_length=1, max_length=32)


class EnactWriterPolicyRequest(StrictInput):
    allowed_roles: list[MembershipRole] = Field(min_length=1, max_length=4)
    expected_revision: StrictInt = Field(ge=0)


class RegisterConnectorRequest(StrictInput):
    namespace: StrictStr = Field(
        min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    )
    connector_kind: ConnectorKind


class CreateContentRequest(StrictInput):
    content_class: ContentClass
    payload: dict[str, Any]
    body: StrictStr = Field(min_length=1, max_length=65_536)

    @field_validator("body")
    @classmethod
    def body_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "body")


class ReviseContentRequest(StrictInput):
    payload: dict[str, Any]
    body: StrictStr = Field(min_length=1, max_length=65_536)
    expected_revision: StrictInt = Field(ge=1)

    @field_validator("body")
    @classmethod
    def body_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "body")


class ContentStatusRequest(StrictInput):
    reason: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    successor_content_id: uuid.UUID | None = None


class IngestContentRequest(StrictInput):
    connector_namespace: StrictStr = Field(min_length=1, max_length=64)
    source_key: StrictStr = Field(min_length=1, max_length=256)
    content_class: ContentClass
    payload: dict[str, Any]
    body: StrictStr = Field(min_length=1, max_length=65_536)
    source_observed_at: datetime | None = None

    @field_validator("body")
    @classmethod
    def body_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "body")

    @field_validator("source_key")
    @classmethod
    def source_key_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "source_key")


class ContentSearchRequest(StrictInput):
    query: StrictStr = Field(min_length=1, max_length=512)
    read_scopes: list[ScopeAlias] = Field(min_length=1, max_length=8)
    limit: StrictInt = Field(default=10, ge=1, le=25)
    include_historical: StrictBool = False

    @field_validator("query")
    @classmethod
    def query_is_postgres_text(cls, value: str) -> str:
        return _reject_nul(value, "query")

    @field_validator("read_scopes")
    @classmethod
    def scopes_are_database_text(cls, values: list[str]) -> list[str]:
        if any("\x00" in value for value in values):
            raise ValueError("read scopes must not contain a NUL character")
        return values
