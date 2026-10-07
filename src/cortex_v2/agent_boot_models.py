"""Strict descriptive manifests and boot-stream enactment inputs.

Scope, author and authority always come from authenticated server context.
These models validate requested data; they do not grant or publish anything.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from .models import StrictInput


class _TextInput(StrictInput):
    @field_validator('*')
    @classmethod
    def postgres_text(cls, value):
        if isinstance(value, str) and '\x00' in value:
            raise ValueError('Boot text must not contain NUL')
        if isinstance(value, list) and any(isinstance(item, str) and '\x00' in item for item in value):
            raise ValueError('Boot text must not contain NUL')
        return value


def _roles(value):
    if any(not role or len(role) > 256 for role in value) or len(set(value)) != len(value):
        raise ValueError('Functional roles must be nonempty distinct registered labels')
    return value


class _Manifest(_TextInput):
    name: StrictStr | None
    description: StrictStr | None
    scope: Literal['project', 'global']
    permission: StrictStr | None
    body_ref: StrictStr | None
    version: StrictStr | None

    @field_validator('body_ref')
    @classmethod
    def canonical_body_ref(cls, value):
        if value is None:
            return value
        path = PurePosixPath(value)
        if (not value or not path.parts or path.is_absolute() or str(path) != value
                or '..' in path.parts or '\\' in value
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError('Boot body reference must be a safe canonical relative path')
        return value


class BootPersonaManifest(_Manifest):
    schema_version: Literal['cortex.boot-persona-manifest.v1']
    functional_roles: list[StrictStr] = Field(min_length=1, max_length=64)
    identity_text: StrictStr = Field(min_length=1, max_length=65536)
    lane: StrictStr | None = None
    not_lane: StrictStr | None = None
    reports_to: StrictStr | None = None

    @field_validator('functional_roles')
    @classmethod
    def registered_role_labels(cls, value):
        return _roles(value)


class BootRuleManifest(_Manifest):
    schema_version: Literal['cortex.boot-rule-manifest.v1']
    title: StrictStr | None
    source_file: StrictStr | None


class BootSkillManifest(_Manifest):
    schema_version: Literal['cortex.boot-skill-manifest.v1']


class _EnactRequest(_TextInput):
    expected_revision: StrictInt = Field(ge=0, le=2147483646)
    state: Literal['active', 'retired']
    source_reference: StrictStr = Field(min_length=1, max_length=4096)


class BootAgentBindRequest(_EnactRequest):
    actor_id: UUID
    identity_id: UUID
    persona_id: UUID
    persona_revision: StrictInt = Field(ge=1, le=2147483647)
    functional_roles: list[StrictStr] = Field(min_length=1, max_length=64)

    @field_validator('functional_roles')
    @classmethod
    def registered_role_labels(cls, value):
        return _roles(value)


class BootEntryBindRequest(_EnactRequest):
    binding_id: UUID | None = None
    subject_kind: Literal['agent', 'functional_role', 'project']
    actor_id: UUID | None = None
    role_slug: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    entry_kind: Literal['skill', 'rule']
    entry_scope_id: UUID
    skill_id: UUID | None = None
    rule_id: UUID | None = None
    bound_revision: StrictInt = Field(ge=1, le=2147483647)
    priority: StrictInt = Field(ge=-2147483648, le=2147483647)

    @model_validator(mode='after')
    def unambiguous_subject_and_entry(self):
        subject_ok = (
            self.subject_kind == 'agent' and self.actor_id is not None and self.role_slug is None
            or self.subject_kind == 'functional_role' and self.actor_id is None and self.role_slug is not None
            or self.subject_kind == 'project' and self.actor_id is None and self.role_slug is None
        )
        entry_ok = (
            self.entry_kind == 'skill' and self.skill_id is not None and self.rule_id is None
            or self.entry_kind == 'rule' and self.rule_id is not None and self.skill_id is None
        )
        if not subject_ok or not entry_ok or self.expected_revision and self.binding_id is None:
            raise ValueError('Boot binding must identify one subject, entry and existing stream')
        return self


class BootPublicationRequest(_EnactRequest):
    publication_id: UUID | None = None
    catalogue_scope_id: UUID
    entry_kind: Literal['skill', 'rule']
    entry_id: UUID
    entry_revision: StrictInt = Field(ge=1, le=2147483647)

    @model_validator(mode='after')
    def existing_stream_required(self):
        if self.expected_revision and self.publication_id is None:
            raise ValueError('An existing publication requires its stream identifier')
        return self
