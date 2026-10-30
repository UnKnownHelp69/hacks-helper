import asyncio
import json
from time import time
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest

from bot.errors import UserError
from bot.services.gigachat import AUTH_URL, GigaChatClient
from bot.services.hack_llm import HackathonLLM
from tests.test_hack_llm import fields, response

TZ = ZoneInfo("Europe/Moscow")


def auth(token="access-secret", expires_at=None):
    return httpx.Response(
        200,
        json={"access_token": token, "expires_at": expires_at or (time() + 1800) * 1000},
    )


async def test_oauth_is_cached_serialized_and_refreshed_before_expiry():
    calls = []

    async def handler(request):
        calls.append(request)
        if str(request.url) == AUTH_URL:
            assert request.headers["Authorization"] == "Basic credentials-secret"
            assert UUID(request.headers["RqUID"]).version == 4
            assert request.content == b"scope=GIGACHAT_API_B2B"
            await asyncio.sleep(0)
            return auth()
        assert request.url.host == "api.giga.chat"
        assert request.headers["Authorization"] == "Bearer access-secret"
        return httpx.Response(200, json={"data": [{"id": "GigaChat"}]})

    client = GigaChatClient(
        "credentials-secret", "GIGACHAT_API_B2B", transport=httpx.MockTransport(handler)
    )
    try:
        await asyncio.gather(*(client.request("GET", "models") for _ in range(3)))
        assert sum(str(call.url) == AUTH_URL for call in calls) == 1
        client._expires_at = time() + 30
        await client.request("GET", "models")
        assert sum(str(call.url) == AUTH_URL for call in calls) == 2
    finally:
        await client.close()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_only_401_refreshes_and_only_once(status):
    calls = []

    def handler(request):
        calls.append(request)
        if str(request.url) == AUTH_URL:
            return auth()
        return httpx.Response(status, text="secret provider body")

    client = GigaChatClient("secret", "GIGACHAT_API_PERS", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.request("GET", "models")
        assert len(calls) == (4 if status == 401 else 2)
    finally:
        await client.close()


async def test_function_extraction_and_provider_credentials_stay_separate():
    calls = []

    def giga(request):
        calls.append(request)
        if str(request.url) == AUTH_URL:
            return auth()
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "GigaChat-2-Pro"}]})
        body = json.loads(request.content)
        assert body["model"] == "GigaChat-2-Pro"
        assert body["function_call"] == {"name": "extract_hackathon"}
        assert body["functions"][0]["parameters"]["required"] == list(fields())
        assert "response_format" not in body and "reasoning" not in body and "provider" not in body
        assert "credentials-secret" not in request.content.decode()
        context = json.loads(body["messages"][1]["content"])
        assert context["timezone"] == TZ.key and context["previous_pages"][0]["text"] == "rules"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "function_call",
                        "message": {
                            "function_call": {
                                "name": "extract_hackathon",
                                "arguments": fields(
                                    start_at="2026-11-05T10:00:00+03:00",
                                    org_chat_url="t.me/ligactf",
                                ),
                            }
                        },
                    }
                ]
            },
        )

    def openrouter(request):
        assert request.headers["Authorization"] == "Bearer router-secret"
        assert json.loads(request.content)["model"] == "test/model"
        return httpx.Response(200, json=response())

    llm = HackathonLLM(
        "router-secret",
        "test/model",
        "https://openrouter.ai/api/v1",
        gigachat_credentials="credentials-secret",
        gigachat_transport=httpx.MockTransport(giga),
        transport=httpx.MockTransport(openrouter),
    )
    try:
        assert await llm.available_models("gigachat") == ["GigaChat", "GigaChat-2-Pro"]
        result = await llm.extract(
            "post",
            "<h1>Хакатон</h1>",
            "https://event.example/",
            TZ,
            selection=("gigachat", "GigaChat-2-Pro"),
            previous_pages=[{"text": "rules"}],
        )
        assert result["start_at"] == "2026-11-05T07:00:00+00:00"
        assert result["org_chat_url"] == "https://t.me/ligactf"
        await llm.extract("post", "", None, TZ)
        assert llm.default_selection == ("openrouter", "test/model")
        assert len(calls) == 3
    finally:
        await llm.close()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "function_call": {
                            "name": "extract_hackathon",
                            "arguments": fields(),
                        }
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "finish_reason": "function_call",
                    "message": {
                        "function_call": {
                            "name": "other_function",
                            "arguments": fields(),
                        }
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "finish_reason": "function_call",
                    "message": {
                        "function_call": {
                            "name": "extract_hackathon",
                            "arguments": fields(team_max=True),
                        }
                    },
                }
            ]
        },
        {"choices": []},
        {"choices": [{"finish_reason": "stop", "message": {"content": "refused"}}]},
    ],
)
async def test_gigachat_rejects_invalid_or_incomplete_output(payload, caplog):
    def handler(request):
        return auth() if str(request.url) == AUTH_URL else httpx.Response(200, json=payload)

    llm = HackathonLLM(
        None,
        "openrouter/free",
        "https://openrouter.ai/api/v1",
        default_provider="gigachat",
        gigachat_credentials="credentials-secret",
        gigachat_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(UserError, match="обычного парсера") as error:
            await llm.extract("post", "", None, TZ)
        assert "credentials-secret" not in str(error.value) + caplog.text
        assert "access-secret" not in str(error.value) + caplog.text
    finally:
        await llm.close()


async def test_gigachat_oauth_failure_is_safe(caplog):
    def handler(request):
        if str(request.url) == AUTH_URL:
            return httpx.Response(401, text="credentials-secret access-secret")
        pytest.fail("No API request after failed auth")

    llm = HackathonLLM(
        None,
        "openrouter/free",
        "https://openrouter.ai/api/v1",
        gigachat_credentials="credentials-secret",
        gigachat_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(UserError, match="HTTP 401") as error:
            await llm.extract("post", "", None, TZ, selection=("gigachat", "GigaChat"))
        assert "secret" not in str(error.value) + caplog.text
        with pytest.raises(UserError, match="Не удалось загрузить"):
            await llm.available_models("gigachat")
    finally:
        await llm.close()


async def test_gigachat_disabled_never_requests_and_explains_setup():
    llm = HackathonLLM(None, "openrouter/free", "https://openrouter.ai/api/v1")
    try:
        with pytest.raises(UserError, match="GIGACHAT_CREDENTIALS"):
            await llm.available_models("gigachat")
        assert await llm.extract("post", "", None, TZ, selection=("gigachat", "GigaChat")) == {}
    finally:
        await llm.close()


async def test_catalog_size_is_bounded_and_failure_is_not_cached():
    calls = 0

    def handler(request):
        nonlocal calls
        if str(request.url) == AUTH_URL:
            return auth()
        calls += 1
        if calls == 1:
            return httpx.Response(200, content=b" " * 2000001)
        return httpx.Response(200, json={"data": [{"id": "Embeddings"}, {"id": "GigaChat-Pro"}]})

    llm = HackathonLLM(
        None,
        "openrouter/free",
        "https://openrouter.ai/api/v1",
        gigachat_credentials="secret",
        gigachat_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(UserError, match="Не удалось загрузить"):
            await llm.available_models("gigachat")
        assert await llm.available_models("gigachat") == ["GigaChat", "GigaChat-Pro"]
    finally:
        await llm.close()
