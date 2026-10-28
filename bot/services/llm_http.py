"""Bound provider responses before decoding untrusted JSON."""

import json
from typing import Any

import httpx


async def bounded_json(
    client: httpx.AsyncClient, method: str, url: str, *, limit: int = 128000, **kwargs: Any
) -> dict[str, Any]:
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise ValueError("Response too large")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("Expected JSON object")
    return result
