"""One stock ASGI lifespan/server and one private resource owner."""
import asyncio
from contextlib import ExitStack,asynccontextmanager
import signal
import time

from starlette.routing import Router
import uvicorn

from cortex_core.api.c11a import _packet,_respond
from .bootstrap_config import BootstrapConfig
from .custody import open_custody
from .resources import ShutdownBudget,open_core_resources
from .services import bind_core_services
from .signed_member import Refusal


class RuntimeApp:
    def __init__(self,config,*,unlock_fd,key_id):
        if type(config) is not BootstrapConfig or config.mode!='serve':raise Refusal('resources_mode')
        self.config=config;self._unlock_fd=unlock_fd;self._key_id=key_id
        self.resources=None;self._bound=None;self._stack=None;self._shutdown=ShutdownBudget(config)
        self.started=False;self.closed=False;self.failure=None;self._used=False
        self._router=Router(default=self._http,lifespan=self._lifespan)

    @property
    def _shutdown_deadline(self):return self._shutdown.deadline

    def begin_shutdown(self,*,deadline=None):
        if self._shutdown_deadline is None:
            now=time.monotonic();cap=self._shutdown.begin(deadline)
            if self.resources is not None:self.resources.begin_shutdown(cap)
            # An in-progress synchronous SQL call already owns this process timer.
            # Shorten it at the first termination signal; never install a competing
            # timer, extend its budget or move SQL into a cancelled thread.
            current,repeat=signal.getitimer(signal.ITIMER_REAL)
            if current:signal.setitimer(signal.ITIMER_REAL,min(current,max(0.000001,cap-now)),repeat)
        return self._shutdown_deadline

    def close(self):
        self.begin_shutdown()
        stack=self._stack;self._stack=None
        try:
            if stack is not None:stack.close()
        except BaseException:
            self.failure='runtime_cleanup'
            raise Refusal('runtime_cleanup') from None
        finally:self.closed=True;self._bound=None

    @asynccontextmanager
    async def _lifespan(self,app):
        if self._used:raise Refusal('runtime_started')
        self._used=True;self._stack=ExitStack()
        try:
            deadline=time.monotonic()+self.config.limits['request_seconds']
            vault=self._stack.enter_context(open_custody(self.config,unlock_fd=self._unlock_fd,key_id=self._key_id))
            if self._shutdown_deadline is not None:raise Refusal('runtime_draining')
            owned=self._stack.enter_context(open_core_resources(self.config,custody=vault,deadline=deadline,_shutdown=self._shutdown))
            self.resources=owned
            if self._shutdown_deadline is not None:
                owned.begin_shutdown(self._shutdown_deadline)
                raise Refusal('runtime_draining')
            bound=bind_core_services(owned);bound.start()
            if self._shutdown_deadline is not None:raise Refusal('runtime_draining')
            self._bound=bound;self.started=True
            yield
        except BaseException:
            if self.failure is None:self.failure='runtime_lifespan'
            raise Refusal('runtime_lifespan') from None
        finally:self.close()

    async def _http(self,scope,receive,send):
        if self._bound is None or self._shutdown_deadline is not None:
            return await _respond(send,503,_packet('core_unavailable'))
        return await self._bound(scope,receive,send)

    async def __call__(self,scope,receive,send):
        return await self._router(scope,receive,send)


class _Server(uvicorn.Server):
    def __init__(self,config,owner):
        super().__init__(config);self.owner=owner;self.shutdown_failed=False

    def handle_exit(self,sig,frame):
        self.owner.begin_shutdown()
        super().handle_exit(sig,frame)

    async def shutdown(self,sockets=None):
        deadline=self.owner.begin_shutdown()
        # Reserve part of this SAME deadline for the lifespan/owned reverse close.
        reserve=min(1,self.owner.config.limits['shutdown_seconds']/4)
        self.config.timeout_graceful_shutdown=max(0,deadline-time.monotonic()-reserve-0.1)
        try:
            async with asyncio.timeout_at(deadline):await super().shutdown(sockets=sockets)
            if self.server_state.tasks:self.shutdown_failed=True
        except BaseException:
            self.shutdown_failed=True
            for task in self.server_state.tasks:task.cancel()
        finally:
            try:self.owner.close()
            except Refusal:self.shutdown_failed=True


def runtime_server(app):
    if type(app) is not RuntimeApp:raise Refusal('resources_unbound')
    config=uvicorn.Config(app,host=app.config.bind['host'],port=app.config.bind['port'],
        loop='asyncio',http='h11',ws='none',lifespan='on',workers=1,reload=False,
        access_log=False,log_config=None,log_level='critical',proxy_headers=False,
        server_header=False,date_header=False,timeout_keep_alive=2,
        timeout_graceful_shutdown=app.config.limits['shutdown_seconds'])
    return _Server(config,app)


async def serve_runtime(config,*,unlock_fd,key_id):
    app=RuntimeApp(config,unlock_fd=unlock_fd,key_id=key_id);server=runtime_server(app)
    try:
        await server.serve()
    finally:app.close()
    if not app.started or app.failure is not None or server.shutdown_failed:
        raise Refusal('runtime_unavailable')
    return 0
