"""One local selection/deadline fence; declaration data grants no effects."""
import asyncio
from dataclasses import dataclass, replace
from uuid import uuid4

from cortex_core.embeddings.projection_revision import (
    request_deadline, check_deadline, CapabilityUnavailable, EmbeddingIdentity,
)


@dataclass(frozen=True)
class AiSelection:
    mode: str
    revision: str
    identity: EmbeddingIdentity | None
    origin: str='https://openrouter.ai:443'
    rerank_enabled: bool=False


class SelectionGate:
    def __init__(self, selection):
        self._active=set()
        self._replace(selection)

    def _replace(self, selection):
        if (not isinstance(selection,AiSelection) or selection.mode not in ('off','hosted')
                or not selection.revision):
            raise ValueError('invalid admitted AI selection')
        # Even identical admitted config bytes get a new non-reusable epoch.
        self.snapshot=replace(selection,revision=uuid4().hex+':'+selection.revision)

    def check(self, *, revision, deadline, mode):
        check_deadline(deadline)
        if self.snapshot.mode!=mode:
            raise CapabilityUnavailable('hosted_disabled' if self.snapshot.mode=='off' else 'selection_mismatch')
        if revision!=self.snapshot.revision:
            raise CapabilityUnavailable('selection_mismatch')

    async def apply(self, selection):
        self._replace(selection)
        for task in tuple(self._active):
            task.cancel()

    async def run(self, callback, *, revision, deadline):
        deadline=request_deadline(deadline)
        self.check(revision=revision,deadline=deadline,mode='hosted')
        task=asyncio.current_task()
        self._active.add(task)
        try:
            value=await callback(deadline)
            self.check(revision=revision,deadline=deadline,mode='hosted')
            return value
        finally:
            self._active.discard(task)
