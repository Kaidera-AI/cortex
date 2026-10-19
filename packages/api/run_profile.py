"""Typed desired run profiles, stored only in agents.capabilities.run_profile.

This is storage, not a model catalogue or permission grant. Native admission must
validate the complete tuple against current credentials/catalogue/authorization.
"""
from copy import deepcopy
import re
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

FIELDS = frozenset({"harness", "provider", "model", "effort", "fast", "permission",
                    "autonomy", "router_mode", "allowed"})
TUPLE_FIELDS = FIELDS - {"router_mode", "allowed"}
PROTECTED_CAPABILITIES = frozenset({"run_profile", "display_name"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,191}\Z")


class FieldInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["set", "auto", "inherit"]
    value: Any = None

    @model_validator(mode="after")
    def explicit_value(self):
        if self.mode == "set" and ("value" not in self.model_fields_set or self.value is None):
            raise ValueError("set requires an explicit non-null value")
        if self.mode != "set" and self.value is not None:
            raise ValueError("auto/inherit cannot carry a pinned or fallback value")
        return self


def validate_value(name: str, value: Any):
    if name == "fast":
        if type(value) is not bool:
            raise ValueError("fast must be a boolean")
    elif name == "permission":
        if value not in ("read-only", "workspace", "full"):
            raise ValueError("unknown permission level")
    elif name == "autonomy":
        if value not in ("interactive", "autonomous"):
            raise ValueError("unknown autonomy")
    elif name == "router_mode":
        if value not in ("off", "auto"):
            raise ValueError("unknown router mode")
    elif name == "allowed":
        if not isinstance(value, list) or len(value) > 32:
            raise ValueError("allowed must contain at most 32 complete tuples")
        for entry in value:
            if not isinstance(entry, dict) or set(entry) != TUPLE_FIELDS:
                raise ValueError("allowed entries require exactly the seven execution fields")
            for key, item in entry.items():
                validate_value(key, item)
        if len({repr(sorted(entry.items())) for entry in value}) != len(value):
            raise ValueError("duplicate allowed tuple")
    elif not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or "://" in value:
        raise ValueError(f"{name} must be a catalogue identifier, never a credential or URL")


def validate_field(name: str, field: FieldInput):
    if name not in FIELDS:
        raise ValueError(f"unknown run-profile field: {name}")
    if name in {"permission", "autonomy", "router_mode", "allowed"} and field.mode == "auto":
        raise ValueError(f"router cannot choose {name}")
    if field.mode == "set":
        validate_value(name, field.value)


class RunProfileWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: StrictInt = Field(ge=0, le=2**53 - 1)
    fields: dict[str, FieldInput] = Field(default_factory=dict, max_length=9)
    display_label: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, fields):
        for name, item in fields.items():
            validate_field(name, item)
        return fields

    @field_validator("display_label")
    @classmethod
    def label_is_display_only(cls, label):
        if label is not None and (label != label.strip() or any(ord(c) < 32 or ord(c) == 127 for c in label)):
            raise ValueError("display label must be nonblank text without control characters")
        return label

    @model_validator(mode="after")
    def nonempty(self):
        if not self.fields and self.display_label is None:
            raise ValueError("provide fields or a display_label")
        return self


class SetBy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    actor: str = Field(min_length=1, max_length=256)
    source: Literal["user", "project_default", "router"]


class StoredField(FieldInput):
    set_by: SetBy
    version: StrictInt = Field(ge=1, le=2**53 - 1)


class StoredProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: StrictInt = Field(ge=1, le=1)
    revision: StrictInt = Field(ge=1, le=2**53 - 1)
    fields: dict[str, StoredField] = Field(max_length=9)

    @field_validator("fields")
    @classmethod
    def fields_are_typed(cls, fields):
        for name, item in fields.items():
            validate_field(name, item)
        return fields

    @model_validator(mode="after")
    def bounded_versions(self):
        if any(field.version > self.revision for field in self.fields.values()):
            raise ValueError("field version exceeds profile revision")
        return self


def reject_profile_capabilities(capabilities: dict):
    if PROTECTED_CAPABILITIES & capabilities.keys():
        raise HTTPException(422, {"code": "profile_write_required", "message":
            "run_profile/display_name require the revision-checked run-profile endpoint"})


def snapshot(project: str, worker: str, capabilities: dict) -> dict:
    raw = capabilities.get("run_profile")
    try:
        profile = StoredProfile.model_validate(raw).model_dump() if raw is not None else {
            "schema_version": 1, "revision": 0, "fields": {}}
    except ValueError:
        raise HTTPException(409, {"code": "invalid_profile", "worker_id": f"{worker}@{project}"}) from None
    return {"project": project, "worker_id": f"{worker}@{project}", "display_label":
            capabilities.get("display_name") or worker, **profile}


def updated(project: str, worker: str, capabilities: dict, body: RunProfileWrite, actor: str):
    before = snapshot(project, worker, capabilities)
    if before["revision"] != body.expected_revision:
        raise HTTPException(409, {"code": "conflict", "expected_revision": body.expected_revision,
                                  "current_revision": before["revision"]})
    if before["revision"] >= 2**53 - 1:
        raise HTTPException(409, {"code": "revision_exhausted"})
    fields = deepcopy(before["fields"])
    for name, item in body.fields.items():
        fields[name] = {**item.model_dump(), "version": fields.get(name, {}).get("version", 0) + 1,
                        "set_by": {"actor": actor, "source": "user"}}
    profile = StoredProfile(schema_version=1, revision=before["revision"] + 1, fields=fields)
    capabilities = deepcopy(capabilities)
    capabilities["run_profile"] = profile.model_dump()
    if body.display_label is not None:
        capabilities["display_name"] = body.display_label
    return capabilities, before, snapshot(project, worker, capabilities)


def require_configured(profile: dict):
    missing = sorted(FIELDS - profile["fields"].keys())
    if missing:
        raise HTTPException(409, {"code": "unconfigured", "worker_id": profile["worker_id"],
                                  "revision": profile["revision"], "missing": missing,
                                  "profile": profile})
    return profile


def validate_imported_profile(row: dict):
    """Privileged vault restore preserves provenance but cannot introduce an untyped profile.

    ON CONFLICT DO NOTHING in the existing importer preserves current rows; an
    import never updates the current profile or circumvents its CAS revision.
    """
    capabilities = row.get("capabilities") or {}
    if isinstance(capabilities, str):
        import json
        try:
            capabilities = json.loads(capabilities)
        except ValueError:
            raise HTTPException(422, {"code": "invalid_profile"}) from None
    if not isinstance(capabilities, dict):
        raise HTTPException(422, {"code": "invalid_profile"})
    if "run_profile" in capabilities:
        try:
            StoredProfile.model_validate(capabilities["run_profile"])
            if "display_name" in capabilities:
                RunProfileWrite(expected_revision=0, display_label=capabilities["display_name"])
        except ValueError:
            raise HTTPException(422, {"code": "invalid_profile"}) from None
