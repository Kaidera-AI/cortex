"""Read-only local health client; public launcher/default binding belongs to C11."""
import asyncio
import ipaddress
import json
import sys
import time
from urllib.parse import urlsplit

import httpx

BUDGET_SECONDS = 2.0
MAX_BODY_BYTES = 16 * 1024


def endpoint_url(value):
    """Accept an explicit literal-loopback origin, before constructing transport."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError()
    if any(c in value for c in "\\?#%"):
        raise ValueError()
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or parts.username is not None
            or parts.password is not None or parts.path not in {"", "/"}):
        raise ValueError()
    address = ipaddress.ip_address(parts.hostname)
    if not address.is_loopback:
        raise ValueError()
    port = parts.netloc.rsplit(":", 1)[-1]
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError()
    host = f"[{address}]" if address.version == 6 else str(address)
    if parts.netloc != f"{host}:{port}":
        raise ValueError()
    return f"{parts.scheme}://{host}:{int(port)}/health"


def arguments(argv):
    if not argv or argv[0] != "status":
        raise ValueError()
    endpoint, json_mode, index = None, False, 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--json" and not json_mode:
            json_mode = True
        elif arg == "--endpoint" and endpoint is None and index + 1 < len(argv):
            index += 1
            endpoint = argv[index]
        else:
            raise ValueError()
        index += 1
    return endpoint_url(endpoint), json_mode


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def validated_receipt(body, http_status):
    value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    fields = {"component", "status", "core_available", "conductor", "reason"}
    if (type(value) is not dict or set(value) != fields
            or type(value["core_available"]) is not bool
            or any(type(value[k]) is not str for k in ("component", "status", "conductor"))
            or (value["reason"] is not None and type(value["reason"]) is not str)):
        raise ValueError()
    states = {"ok": (200, True, "healthy", (None,)),
              "degraded": (200, True, "supervisor_down", (None,)),
              "unavailable": (503, False, "core_unavailable", ("core_unavailable", "timeout"))}
    expected = states.get(value["status"])
    if (value["component"] != "cortex" or expected is None
            or (http_status, value["core_available"], value["conductor"]) != expected[:3]
            or value["reason"] not in expected[3]):
        raise ValueError()
    return {key: value[key] for key in ("component", "status", "core_available", "conductor", "reason")}


def check_deadline(deadline):
    # A blocking callback or parser can prevent asyncio's timer from firing.
    if time.monotonic() >= deadline:
        raise TimeoutError()


async def read_health(url, transport):
    deadline = time.monotonic() + BUDGET_SECONDS
    async with asyncio.timeout(BUDGET_SECONDS):
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                    follow_redirects=False, verify=True, timeout=BUDGET_SECONDS) as client:
            async with client.stream("GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                check_deadline(deadline)
                if (response.status_code not in {200, 503}
                        or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
                        or response.headers.get("content-encoding", "identity").lower() != "identity"):
                    raise ValueError()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    check_deadline(deadline)
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise ValueError()
                    body.extend(chunk)
                health = validated_receipt(body, response.status_code)
                check_deadline(deadline)
        check_deadline(deadline)
    return health


def emit(out, json_mode, health, error):
    packet = {"schema": "cortex.status.v1", "health": health, "client_error": error}
    if json_mode:
        print(json.dumps(packet, separators=(",", ":")), file=out)
    else:
        print("Cortex status: " + (health["status"] if health else "unavailable"), file=out)
        print("Core: " + ("available" if health["core_available"] else "unavailable") if health else "Core: unknown", file=out)
        print("Conductor: " + (health["conductor"] if health else "unknown"), file=out)
        print("Reason: " + (health["reason"] or "none") if health else "Client error: " + error, file=out)


async def run(argv, *, transport=None, out=None):
    out = sys.stdout if out is None else out
    health, error, code = None, None, 64
    json_mode = "--json" in argv
    try:
        url, json_mode = arguments(argv)
    except (ValueError, TypeError):
        error = "endpoint_refused"
    else:
        code = 2
        try:
            health = await read_health(url, transport)
            code = {"ok": 0, "degraded": 1, "unavailable": 2}[health["status"]]
        except (TimeoutError, httpx.TimeoutException):
            error = "timeout"
        except (ValueError, UnicodeError):
            error = "invalid_response"
        except Exception:
            # Never forward dependency exception text or fabricate server health.
            error = "connection_unavailable"
    emit(out, json_mode, health, error)
    return code


def main(argv=None, *, transport=None, out=None):
    try:
        return asyncio.run(run(sys.argv[1:] if argv is None else argv, transport=transport, out=out))
    except KeyboardInterrupt:
        return 130
