"""Record-local projection evidence; no global cursor or waiting behavior."""
from dataclasses import dataclass
from typing import Literal
from uuid import UUID


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
