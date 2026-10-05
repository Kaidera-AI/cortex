"""Minimal stdlib HTTP transport for the v2 client.

The runtime image ships only asyncpg/fastapi/uvicorn, so the client uses
``urllib.request`` instead of adding an HTTP dependency. One request, one
response, no retries: retry policy belongs to the caller because only the
caller knows whether an effect is safe to repeat.

Credentials stay at the selected origin. Redirects remain response outcomes;
environment proxy settings never select a different credential recipient.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import CortexTransportError

DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    query: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> HttpResponse:
    target = url
    if query:
        separator = "&" if "?" in url else "?"
        target = f"{url}{separator}{urllib.parse.urlencode(dict(query))}"
    data = None
    request_headers = {"Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(
        target, data=data, headers=request_headers, method=method
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirects()
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                headers={
                    key.lower(): value for key, value in response.headers.items()
                },
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:  # non-2xx is a normal API outcome
        return HttpResponse(
            status=exc.code,
            headers={
                key.lower(): value for key, value in (exc.headers or {}).items()
            },
            body=exc.read(),
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CortexTransportError(
            f"request to {method} {url} failed: {exc}"
        ) from exc
