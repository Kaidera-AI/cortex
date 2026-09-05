"""Boot persona contract models — cortex.persona.v2.

Versioned Pydantic models for the PersonaPayload emitted by /boot/{agent}.
These are additive: the existing boot/surface_version response fields are
unchanged. PersonaPayload is attached under the new ``persona`` key.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SkillManifestEntry(BaseModel):
    """A single skill entry in the boot persona manifest."""

    skill_slug: str
    name: Optional[str] = None
    description: Optional[str] = None
    scope: str = "project"
    permission: Optional[str] = None
    version: str = "1"
    body_ref: Optional[str] = None


class HarnessAdapter(BaseModel):
    """Identifies the harness this agent runs in for this boot context."""

    harness: str
    entry_file: str
    notes: Optional[str] = None


class PersonaPayload(BaseModel):
    """The structured persona payload attached to /boot/{agent} responses.

    schema_version identifies the contract. The existing ``boot`` (str) and
    ``surface_version`` (str) fields in the response are unaffected — this
    payload lives under the separate ``persona`` key.
    """

    schema_version: str = "cortex.persona.v2"
    project: str
    agent: str
    agent_identity: str
    role: Optional[str] = None
    identity_text: str
    skills: List[SkillManifestEntry] = Field(default_factory=list)
    rules: List[Dict[str, Any]] = Field(default_factory=list)
    pending_handoffs: List[Dict[str, Any]] = Field(default_factory=list)
    harness: Optional[HarnessAdapter] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
