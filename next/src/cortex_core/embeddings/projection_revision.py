"""Driver-free search contracts, request guards and record-local projection evidence."""
from dataclasses import dataclass
import asyncio
import hashlib
import json
import math
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class Scope:
    tenant_id: UUID
    project_id: UUID


@dataclass(frozen=True)
class EmbeddingIdentity:
    provider: str
    model: str
    version: str
    dimensions: int
    preprocessing: str

    @property
    def key(self) -> str:
        if self.dimensions != 768 or not all(
            isinstance(v, str) and v.strip()
            for v in (self.provider, self.model, self.version, self.preprocessing)
        ):
            raise ValueError("Search requires a complete identity with 768 dimensions")
        return hashlib.sha256(
            json.dumps(
                [
                    self.provider,
                    self.model,
                    self.version,
                    self.dimensions,
                    self.preprocessing,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Freshness:
    identity: str
    indexed_records: int
    pending_records: int

    @property
    def status(self) -> str:
        return "lagging" if self.pending_records else "current"


@dataclass(frozen=True)
class Hit:
    record_id: str
    kind: str
    source_revision: int
    distance: float


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[Hit, ...]
    freshness: Freshness


class CapabilityUnavailable(RuntimeError):
    code = "capability_unavailable"
    capability = "search"

    def __init__(self, reason: str, freshness: Freshness | None = None):
        super().__init__(reason)
        self.reason = reason
        self.freshness = freshness


class CoreUnavailable(RuntimeError):
    code = "core_unavailable"


class StaleEmbedding(RuntimeError):
    code = "stale_embedding"


def request_deadline(deadline):
    now=asyncio.get_running_loop().time()
    if type(deadline) not in (int,float) or not math.isfinite(deadline):
        raise CapabilityUnavailable('request_timeout')
    return min(deadline,now+10)


def check_deadline(deadline):
    if asyncio.get_running_loop().time()>=deadline:
        raise CapabilityUnavailable('request_timeout')


@dataclass(frozen=True)
class ProjectionRevision:
    state: Literal['visible', 'pending', 'unavailable']
    record_id: UUID
    requested_revision: int
    indexed_revision: int | None
    identity: str
    generation: UUID | None = None
    reason: str = ''


def validate_target(target, recheck):
    if (type(target) is not dict or set(target) != {'record_id', 'revision'}
        or not isinstance(target['record_id'], UUID)
        or type(target['revision']) is not int or not 0 < target['revision'] < 2**63):
        raise ValueError('A committed record-local revision target is required')
    if not callable(recheck):
        raise TypeError('Explicit current Core permission recheck is required')
    return target['record_id'], target['revision']


async def finish_observation(recheck, subject, scope, observation):
    # Binding is once per snapshot; this is the caller's separate Core _current
    # adapter, after the read transaction exits, never a second bind_scope call.
    if await recheck(subject, scope) is not True:
        raise PermissionError('Current Core permission recheck refused')
    return observation
