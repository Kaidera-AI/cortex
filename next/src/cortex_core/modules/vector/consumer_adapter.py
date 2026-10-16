"""Synchronous C07 sink over the single owned async private client.

The existing consumer runs in its worker thread/process, never an API event loop.
No credential, socket, event loop or client is retained between effects.
"""
import asyncio

from .projection import ProjectionBinding, _event
from .qdrant import ProtocolFailure
from .qdrant_client import PrivateClient


class QdrantSink:
    def __init__(self, binding, client_factory, embed):
        if type(binding) is not ProjectionBinding or not callable(client_factory) or not callable(embed):
            raise ValueError('invalid_projection_sink')
        self.binding, self.client_factory, self.embed = binding, client_factory, embed

    def apply(self, event, generation):
        try:
            if type(generation) is not int or generation != self.binding.consumer_generation:
                raise ValueError()
            _event(self.binding, event)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise ValueError()  # A synchronous C07 worker cannot block an API loop.
            return asyncio.run(self._apply(event))
        except Exception:
            raise ProtocolFailure('qdrant_projection_unavailable') from None

    async def _apply(self, event):
        client = self.client_factory()
        if type(client) is not PrivateClient:
            raise ValueError()
        async with client:
            async def operation(deadline):
                # Provider work, queue, HTTP and readback share the client's one
                # ownership/deadline bound; cancellation never leaves an orphan.
                vector = await self.embed(self.binding.identity, event.payload)
                from .projection import build_point
                desired = build_point(self.binding, event, vector)
                return await client._apply_projection(self.binding, event, desired, deadline)
            return await client._owned(operation, None)
