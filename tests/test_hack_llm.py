import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from bot.errors import UserError
from bot.services.hack_llm import Extraction, HackathonLLM, page_context

TZ = ZoneInfo("Europe/Moscow")


def fields(**overrides):
    result = dict.fromkeys(Extraction.model_fields)
    result.update(is_hackathon=True, title="AI Hack", team_max=5, checkpoints=[], follow_urls=[])
    result.update(overrides)
    return result


def response(data=None, **overrides):
    choice = {"finish_reason": "stop", "message": {"content": json.dumps(data or fields())}}
    choice.update(overrides)
    return {"choices": [choice]}


def client(handler, key="test-secret", *, mandatory=False):
    def route(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "test/extraction:free",
                            "supported_parameters": ["structured_outputs", "reasoning"],
                            "reasoning": {"mandatory": mandatory},
                        }
                    ]
                },
            )
        return handler(request)

    return HackathonLLM(
        key,
        "openrouter/free",
        "https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(route),
    )


@pytest.mark.parametrize(
    ("field", "url", "expected"),
    [
        ("org_chat_url", "t.me/ligactf", "https://t.me/ligactf"),
        ("org_chat_url", "telegram.me/ligactf", "https://telegram.me/ligactf"),
        ("org_chat_url", "t.me/registration_bot?start=null", None),
        ("site_url", "лигацтф.рус", "https://лигацтф.рус/"),
        ("spec_url", "event.example/rules.pdf", "https://event.example/rules.pdf"),
    ],
)
def test_bare_domains_are_normalized_before_url_and_organizer_validation(field, url, expected):
    assert getattr(Extraction.model_validate(fields(**{field: url})), field) == expected


async def test_structured_request_and_normalized_dates():
    def handler(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        assert body["provider"] == {"require_parameters": True, "sort": "latency"}
        assert body["reasoning"] == {"effort": "none"}
        schema = body["response_format"]["json_schema"]
        assert schema["strict"] and not schema["schema"]["additionalProperties"]
        assert set(schema["schema"]["required"]) == set(Extraction.model_fields)
        assert "team_min" not in schema["schema"]["properties"]
        assert body["max_tokens"] == 8000
        context = json.loads(body["messages"][1]["content"])
        assert context["timezone"] == TZ.key
        assert context["page"]["links"] == [
            {"url": "https://event.example/rules", "label": "Правила"}
        ]
        return httpx.Response(
            200,
            json=response(
                fields(
                    start_at="2026-11-05T10:00:00+03:00",
                    checkpoints=[{"title": "Demo", "at": "2026-11-06T18:00:00Z"}],
                    repo="https://github.com/team/solution",
                    org_chat_url="https://t.me/registration_bot?start=null",
                )
            ),
        )

    llm = client(handler)
    try:
        result = await llm.extract(
            "Хакатон", '<a href="/rules">Правила</a>', "https://event.example/", TZ
        )
        assert result["start_at"] == "2026-11-05T07:00:00+00:00"
        assert result["repo"] == "team/solution"
        assert result["checkpoints"][0]["at"] == "2026-11-06T18:00:00+00:00"
        assert "end_at" not in result
        assert "org_chat_url" not in result
    finally:
        await llm.close()


@pytest.mark.parametrize(
    "payload",
    [
        response(fields(team_max=True)),
        response(fields(team_max=101)),
        response(fields(start_at="2026-11-05T10:00:00")),
        response(fields(start_at="0001-01-01T00:00:00+14:00")),
        response(fields(org_chat_url="http://127.0.0.1/")),
        response(fields(org_chat_url="localhost/ligactf")),
        response(fields(org_chat_url="t.me@127.0.0.1/ligactf")),
        response(fields(site_url="127.0.0.1/private")),
        response(fields(spec_url="event.internal/rules")),
        response(fields(site_url="javascript:alert(1)")),
        response(fields(editing_id=7)),
        response(fields(checkpoints=[{"title": "x", "at": "bad date"}])),
        response(finish_reason="length"),
        {"choices": [{"finish_reason": "stop", "message": {"refusal": "No"}}]},
        {"choices": []},
        {"choices": [None]},
        {"choices": [["stop"]]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "invalid json"}}]},
    ],
)
async def test_untrusted_invalid_or_incomplete_responses_are_rejected(payload):
    llm = client(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(UserError, match="обычного парсера"):
            await llm.extract("post", "", None, TZ)
    finally:
        await llm.close()


@pytest.mark.parametrize("status", [401, 402, 429, 500, 302])
async def test_api_failure_has_no_retry_or_secret_in_error(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret provider response")

    llm = client(handler)
    try:
        with pytest.raises(UserError) as error:
            await llm.extract("post", "", None, TZ)
        assert "secret" not in str(error.value)
        assert len(calls) == 1
    finally:
        await llm.close()


async def test_timeout_is_explained_without_exposing_exception(caplog):
    def timeout(request):
        raise httpx.ReadTimeout("secret", request=request)

    llm = client(timeout)
    try:
        with pytest.raises(UserError, match="две минуты") as error:
            await llm.extract("post", "", None, TZ)
        assert "secret" not in str(error.value) + caplog.text
        assert "timed out" in caplog.text
    finally:
        await llm.close()


async def test_response_size_limit():
    llm = client(lambda request: httpx.Response(200, content=b"x" * 128001))
    try:
        with pytest.raises(UserError):
            await llm.extract("post", "", None, TZ)
    finally:
        await llm.close()


async def test_disabled_and_busy_clients_do_not_make_requests():
    def unexpected(request):
        pytest.fail("No API call expected")

    llm = client(unexpected, key=None)
    try:
        assert await llm.extract("post", "", None, TZ) == {}
        llm.configured = True
        async with llm._slots, llm._slots:
            with pytest.raises(UserError, match="занята"):
                await llm.extract("post", "", None, TZ)
    finally:
        await llm.close()


def test_context_is_bounded_and_excludes_scripts_and_local_links():
    context = page_context(
        "<script>secret script</script><style>hidden</style>"
        '<a href="http://localhost/">local</a><a href="/rules">Rules</a>'
        + "<p>"
        + "x" * 60000
        + "</p>",
        "https://event.example/",
    )
    assert len(context["text"]) == 30000
    assert "secret script" not in json.dumps(context)
    assert context["links"] == [{"url": "https://event.example/rules", "label": "Rules"}]


async def test_free_router_accepts_endpoint_with_mandatory_reasoning():
    requests = []

    def mandatory_endpoint(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["max_tokens"] >= 8000  # Reasoning tokens share this output budget.
        reasoning = body.get("reasoning", {})
        if reasoning.get("effort") == "none" or reasoning.get("enabled") is False:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Reasoning is mandatory for this endpoint and cannot be disabled."
                        )
                    }
                },
            )
        return httpx.Response(200, json=response(fields(title="ИИкузница", team_max=1)))

    llm = client(mandatory_endpoint, mandatory=True)
    try:
        result = await llm.extract(
            "https://иикузница.рф", "<h1>Хакатон ИИкузница</h1>", "https://иикузница.рф/", TZ
        )
        assert result["title"] == "ИИкузница" and result["team_max"] == 1
        assert len(requests) == 1
    finally:
        await llm.close()


async def test_free_catalog_prefers_compatible_free_models_and_caches_fallback_list():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":

            def model(name, mandatory):
                return {
                    "id": name,
                    "supported_parameters": ["structured_outputs", "reasoning"],
                    "reasoning": {"mandatory": mandatory},
                }

            return httpx.Response(
                200,
                json={
                    "data": [
                        model("mandatory:free", True),
                        model("paid", False),
                        model("first:free", False),
                        model("second:free", False),
                    ]
                },
            )
        body = json.loads(request.content)
        assert body["models"] == ["first:free", "second:free"]
        assert body["reasoning"] == {"effort": "none"}
        return httpx.Response(200, json=response())

    llm = HackathonLLM(
        "test-secret",
        "openrouter/free",
        "https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        for _ in range(2):
            assert (await llm.extract("post", "", None, TZ))["is_hackathon"]
        assert [request.method for request in calls] == ["GET", "POST", "POST"]
    finally:
        await llm.close()


async def test_model_catalog_filters_unsupported_and_orders_free_first():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "a/paid", "supported_parameters": ["structured_outputs"]},
                    {"id": "b/free:free", "supported_parameters": ["structured_outputs"]},
                    {"id": "c/no-json", "supported_parameters": []},
                    {"id": "d/unsafe\nline", "supported_parameters": ["structured_outputs"]},
                    None,
                ]
            },
        )

    llm = HackathonLLM(
        "secret",
        "openrouter/free",
        "https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await llm.available_models("openrouter") == [
            "openrouter/free",
            "b/free:free",
            "a/paid",
        ]
    finally:
        await llm.close()


async def test_compatible_api_does_not_receive_openrouter_specific_options():
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "selected/model"
        assert "provider" not in body and "reasoning" not in body and "models" not in body
        return httpx.Response(200, json=response())

    llm = HackathonLLM(
        "secret",
        "default/model",
        "https://compatible.example/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (
            await llm.extract("post", "", None, TZ, selection=("openrouter", "selected/model"))
        )["is_hackathon"]
        assert llm.model == "default/model"
    finally:
        await llm.close()


async def test_audit_receives_issues_and_preserves_null_to_retract_uncertain_date():
    def handler(request):
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert context["review_issues"] == ["Registration deadline is after event start"]
        assert context["current"]["registration_end"] == "2026-11-06T20:59:00+00:00"
        return httpx.Response(200, json=response(fields(registration_end=None, date_evidence=[])))

    llm = client(handler)
    try:
        result = await llm.extract(
            "post",
            "",
            None,
            TZ,
            current={"registration_end": "2026-11-06T20:59:00+00:00"},
            review_issues=["Registration deadline is after event start"],
        )
        assert "registration_end" in result and result["registration_end"] is None
    finally:
        await llm.close()


@pytest.mark.parametrize("source_url", ["https://event.example/", "https://invented.example/"])
async def test_audit_independently_checks_quotes_and_removes_unsupported_time(source_url):
    def handler(request):
        body = json.loads(request.content)
        assert "date_evidence" in body["response_format"]["json_schema"]["schema"]["required"]
        return httpx.Response(
            200,
            json=response(
                fields(
                    registration_end="2026-10-19T20:59:00+00:00",
                    start_at="2026-11-13T07:00:00+00:00",
                    end_at="2026-11-16T20:59:00+00:00",
                    date_evidence=[
                        {
                            "field": "registration_end",
                            "quote": "Регистрация закрыта",
                            "source_url": source_url,
                        },
                        {
                            "field": "start_at",
                            "quote": "13 — 16 ноября 2026",
                            "source_url": source_url,
                        },
                        {
                            "field": "end_at",
                            "quote": "13 — 16 ноября 2026",
                            "source_url": source_url,
                        },
                    ],
                )
            ),
        )

    llm = client(handler)
    try:
        result = await llm.extract(
            "https://event.example/",
            "",
            None,
            TZ,
            previous_pages=[
                {
                    "url": "https://event.example/",
                    "text": "Регистрация закрыта. 13 — 16 ноября 2026",
                }
            ],
            review_issues=["Проверить даты"],
        )
        assert result["registration_end"] is None
        assert "date_evidence" not in result
        if source_url == "https://event.example/":
            assert result["start_at"] == "2026-11-12T21:00:00+00:00"
            assert result["end_at"] == "2026-11-16T20:59:00+00:00"
        else:
            assert result["start_at"] is None and result["end_at"] is None
    finally:
        await llm.close()
