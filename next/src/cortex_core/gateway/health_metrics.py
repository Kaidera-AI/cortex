"""Local pull-only telemetry. Core ports supply observations, never credentials."""
import asyncio
from dataclasses import dataclass
import ipaddress
import math
import time

from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Router

from cortex_core.conductor.metrics import Metrics


@dataclass(frozen=True)
class CoreSample:
    db_bytes: int
    embed_backlog: int


def loopback(host):
    try:
        return isinstance(host, str) and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_binding(host, port):
    if not loopback(host):
        raise ValueError("Telemetry binding must be literal loopback")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid telemetry port")


class TelemetryGateway:
    """Dedicated minimal surface; C11 supplies the existing server and Core ports."""

    def __init__(self, metrics, read_health, read_sample, *, host="127.0.0.1", port=0,
                 timeout_seconds=0.25):
        validate_binding(host, port)
        if (type(metrics) is not Metrics or not callable(read_health) or not callable(read_sample)):
            raise ValueError("Existing Metrics and both Core readers are required")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 2):
            raise ValueError("Invalid telemetry time budget")
        self.metrics, self.read_health, self.read_sample = metrics, read_health, read_sample
        self.host, self.port, self.timeout_seconds = host, port, timeout_seconds
        self.router = Router(routes=[Route(path, handler, methods=["GET"]) for path, handler in
                                     (("/health", self._health), ("/metrics", self._metrics))])
        self.app = self._application

    def serve(self, runner):
        """The integration owner calls this with the existing ASGI server; no default."""
        validate_binding(self.host, self.port)
        return runner(self.app, host=self.host, port=self.port,
                      proxy_headers=False, forwarded_allow_ips="")

    async def _application(self, scope, receive, send):
        peer = scope.get("client")
        if scope["type"] == "http" and not (peer and loopback(peer[0])):
            response = JSONResponse({"error": {"code": "permission_denied", "reason": "loopback_required"}},
                                    status_code=403, headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        await self.router(scope, receive, send)

    @staticmethod
    def _deadline(deadline):
        # Also refuses a late callback that stalled the loop or suppressed cancellation.
        if time.monotonic() >= deadline:
            raise TimeoutError()

    @staticmethod
    def _health_value(value):
        if not isinstance(value, dict):
            raise ValueError("Invalid Core health receipt")
        available, state = value.get("core_available"), value.get("state")
        if (type(available) is not bool or state not in {"healthy", "supervisor_down", "core_unavailable"}
                or available != (state != "core_unavailable")):
            raise ValueError("Invalid Core health receipt")
        return {"component": "cortex", "status": ("ok" if state == "healthy" else
                "degraded" if available else "unavailable"), "core_available": available,
                "conductor": state, "reason": None if available else "core_unavailable"}

    @staticmethod
    def _health_error(reason):
        return JSONResponse({"component": "cortex", "status": "unavailable", "core_available": False,
                             "conductor": "core_unavailable", "reason": reason},
                            status_code=503, headers={"Cache-Control": "no-store"})

    @staticmethod
    def _metrics_error(reason):
        return JSONResponse({"error": {"code": "capability_unavailable", "capability": "metrics",
                                        "reason": reason}}, status_code=503,
                            headers={"Cache-Control": "no-store"})

    async def _health(self, request):
        deadline = time.monotonic() + self.timeout_seconds
        try:
            async with asyncio.timeout(self.timeout_seconds):
                value = self._health_value(await self.read_health())
                self._deadline(deadline)
            return JSONResponse(value, status_code=200 if value["core_available"] else 503,
                                headers={"Cache-Control": "no-store"})
        except TimeoutError:
            return self._health_error("timeout")
        except Exception:
            return self._health_error("core_unavailable")

    async def _metrics(self, request):
        deadline = time.monotonic() + self.timeout_seconds
        try:
            async with asyncio.timeout(self.timeout_seconds):
                health = self._health_value(await self.read_health())
                self._deadline(deadline)
                if not health["core_available"]:
                    return self._metrics_error("core_unavailable")
                sample = await self.read_sample()
                self._deadline(deadline)
                if type(sample) is not CoreSample:
                    raise ValueError("Invalid Core metric receipt")
                self.metrics.update_core(sample.db_bytes, sample.embed_backlog)
                text = self.metrics.export()
                self._deadline(deadline)
            return Response(text, media_type="text/plain; version=0.0.4",
                            headers={"Cache-Control": "no-store"})
        except TimeoutError:
            return self._metrics_error("timeout")
        except Exception:
            return self._metrics_error("sample_unavailable")
