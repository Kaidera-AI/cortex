"""X02 selectable RED stub; no production process/service-manager interface."""

import asyncio


async def eventually(predicate, seconds=5):
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if hasattr(result, "__await__"):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.01)
    return False


class Child:
    receipt = {"kind": "not_implemented"}
    pid = fence = process = None

    @classmethod
    async def spawn(cls, dsn, installation, *, lease_seconds=0.5, mode="manual", crash=False):
        return cls()

    async def request(self, kind, **values):
        return self.receipt

    async def kill(self):
        return self.receipt

    async def close(self):
        pass


class Manager:
    def __init__(self, dsn, installation, *, lease_seconds=0.5, max_restarts=3, crash=False):
        self.state = "not_implemented"
        self.children = []
        self.events = []
        self.child = None
        self.task = None

    async def start(self):
        return {"kind": "not_implemented"}

    async def request_stop(self):
        pass

    async def close(self):
        pass


class Consumer:
    def __init__(self, pool, *, pause_at=None):
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def advance(self):
        return 0
