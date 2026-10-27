import json

import pytest

from bot.errors import UserError
from bot.services.hack_import import (
    PublicResolver,
    extract_page,
    extract_post,
    fetch_public_page,
    public_url,
)
from tests.test_users import MSK


def test_extract_russian_post_dates_limits_and_links():
    result = extract_post(
        "Хакатон нашей команды\nРегистрация до 01.11.2026 23:59\n"
        "Начало: 05.11.2026 10:00\nОкончание: 07.11.2026 18:00\n"
        "Команды от 2 до 4 человек\nЧекпоинт: 06.11.2026 12:00\n"
        "https://hack.example/event\nhttps://github.com/team/solution\n"
        "Чат организаторов: https://t.me/organizers",
        MSK,
    )
    assert result["title"] == "Хакатон нашей команды"
    assert result["start_at"] == "2026-11-05T07:00:00+00:00"
    assert result["end_at"] == "2026-11-07T15:00:00+00:00"
    assert (result["team_min"], result["team_max"]) == (2, 4)
    assert result["repo"] == "team/solution"
    assert result["org_chat_url"] == "https://t.me/organizers"
    assert result["checkpoints"][0]["at"] == "2026-11-06T09:00:00+00:00"


def test_extract_structured_event_and_do_not_invent_registration_deadline():
    event = {
        "@type": "Event",
        "name": "Hackathon 2026",
        "startDate": "2026-11-05T10:00:00+03:00",
        "endDate": "2026-11-07T18:00:00+03:00",
        "minTeamSize": 2,
        "maxTeamSize": 5,
    }
    html = f'<script type="application/ld+json">{json.dumps({"@graph": [event]})}</script>'
    result = extract_page(html, "https://hack.example/", MSK)
    assert result["start_at"] == "2026-11-05T07:00:00+00:00"
    assert result["team_max"] == 5 and result["is_hackathon"]
    assert "registration_end" not in result


def test_missing_post_data_stays_empty():
    result = extract_post("Скоро хакатон, подробности появятся позже", MSK)
    assert "start_at" not in result and "team_min" not in result
    result = extract_post("Хакатон\nНачало 5 ноября 2026 в 10:00", MSK)
    assert result["start_at"] == "2026-11-05T07:00:00+00:00"


def test_russian_date_range_and_invalid_deep_metadata():
    result = extract_post("Хакатон команды\nПройдёт 3–5 ноября 2026\nКоманды: 2–4 человека", MSK)
    assert result["start_at"] == "2026-11-02T21:00:00+00:00"
    assert result["end_at"] == "2026-11-05T20:59:00+00:00"
    assert result["team_min"] == 2
    html = (
        '<h1>Хакатон</h1><script type="application/ld+json">'
        + "[" * 2000
        + "0"
        + "]" * 2000
        + "</script>"
    )
    assert extract_page(html, "https://hack.example/", MSK)["is_hackathon"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "https://localhost/",
        "https://app.internal/",
        "https://user:pass@hack.example/",
        "https://hack.example:8000/",
        "http://2130706433/",
        "https://hack.example/\nsecret",
    ],
)
def test_private_destinations_and_credentials_are_rejected(url):
    with pytest.raises(UserError):
        public_url(url)


async def test_dns_rebinding_and_mixed_private_records_are_rejected(monkeypatch):
    resolver = PublicResolver()

    async def resolve(*args):
        return [{"host": "8.8.8.8"}, {"host": "127.0.0.1"}]

    monkeypatch.setattr(resolver.delegate, "resolve", resolve)
    try:
        with pytest.raises(OSError, match="Non-public"):
            await resolver.resolve("hack.example", 443)
    finally:
        await resolver.close()


@pytest.mark.parametrize("mode", ["private_redirect", "oversized", "non_html"])
async def test_fetch_rejects_unsafe_redirects_and_unbounded_or_non_page_content(monkeypatch, mode):
    requests = []

    class Response:
        status = 302 if mode == "private_redirect" else 200
        headers = {"Location": "http://127.0.0.1/secret"} if mode == "private_redirect" else {}
        content_type = "application/pdf" if mode == "non_html" else "text/html"
        charset = "utf-8"

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield b"a" * 1_000_001

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, url, **kwargs):
            requests.append(url)
            return Response()

    monkeypatch.setattr("bot.services.hack_import.aiohttp.ClientSession", Session)
    with pytest.raises(UserError):
        await fetch_public_page("https://hack.example/")
    assert requests == ["https://hack.example/"]
