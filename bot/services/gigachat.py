"""GigaChat OAuth lifecycle. Credentials never enter extraction prompts or storage."""

from __future__ import annotations

import asyncio
import math
import ssl
from time import time
from typing import Any
from uuid import uuid4

import httpx

from bot.services.llm_http import bounded_json

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
BASE_URL = "https://api.giga.chat/v1/"


class GigaChatClient:
    def __init__(
        self,
        credentials: str | None,
        scope: str,
        ca_bundle_file: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.configured = bool(credentials)
        self._credentials = credentials
        self._scope = scope
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        # Add the official CA bundle to system trust; never disable TLS verification.
        verify = ssl.create_default_context()
        if ca_bundle_file:
            verify.load_verify_locations(cafile=ca_bundle_file)
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            verify=verify,
            timeout=httpx.Timeout(115, connect=10),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def token(self) -> str:
        async with self._lock:
            if self._token and self._expires_at > time() + 60:
                return self._token
            result = await bounded_json(
                self._client,
                "POST",
                AUTH_URL,
                headers={
                    "Authorization": f"Basic {self._credentials}",
                    "RqUID": str(uuid4()),
                    "Accept": "application/json",
                },
                data={"scope": self._scope},
            )
            token = result["access_token"]
            expires_at = float(result["expires_at"]) / 1000
            if not isinstance(token, str) or not token or not math.isfinite(expires_at):
                raise ValueError("Invalid OAuth response")
            if expires_at <= time():
                raise ValueError("Expired OAuth token")
            self._token, self._expires_at = token, expires_at
            return token

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self.token()
        for attempt in range(2):
            try:
                return await bounded_json(
                    self._client,
                    method,
                    path,
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 401 or attempt:
                    raise
                # One refresh only; another coroutine may already have refreshed the token.
                async with self._lock:
                    if self._token == token:
                        self._token = None
                token = await self.token()
        raise RuntimeError("Unreachable")
