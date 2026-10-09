"""Current OpenRouter hosted embedding boundary; no credential/config fallback."""

import asyncio
import json

import httpx
from cortex_core.embeddings.pg_search import CapabilityUnavailable, vector_literal

ENDPOINT = "https://openrouter.ai/api/v1/embeddings"


class HostedProvider:
    def __init__(self, client: httpx.AsyncClient, resolve_key, *, timeout_seconds=8):
        if not callable(resolve_key) or not 0 < timeout_seconds <= 8:
            raise ValueError(
                "A current credential resolver and bounded timeout are required"
            )
        self.client = client
        self.resolve_key = resolve_key
        self.timeout_seconds = timeout_seconds

    async def embed(self, text: str, identity):
        identity.key
        if identity.provider != "openrouter":
            raise CapabilityUnavailable("provider_not_supported")
        if not isinstance(text, str) or not text or len(text.encode()) > 16 * 1024:
            raise ValueError("Query must be nonempty and at most 16 KiB UTF-8")
        try:
            # One total budget includes credential lookup and streamed response.
            async with asyncio.timeout(self.timeout_seconds):
                key = await self.resolve_key(identity.provider)
                if not isinstance(key, str) or not key.strip():
                    raise CapabilityUnavailable("provider_credentials_unavailable")
                async with self.client.stream(
                    "POST",
                    ENDPOINT,
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": identity.model,
                        "input": text,
                        "dimensions": identity.dimensions,
                    },
                    timeout=self.timeout_seconds,
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > 256 * 1024:
                            raise ValueError("Provider response exceeds bound")
                        body.extend(chunk)
                payload = json.loads(body)
                rows = payload["data"]
                if not isinstance(rows, list) or len(rows) != 1:
                    raise ValueError("Expected one query embedding")
                vector = rows[0]["embedding"]
                vector_literal(vector, identity.dimensions)
                return [float(v) for v in vector]
        except CapabilityUnavailable:
            raise
        except Exception:
            # Credential/provider error bodies may contain secrets; never forward.
            raise CapabilityUnavailable("provider_unavailable") from None
