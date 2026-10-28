import json

import pytest

from bot.errors import UserError
from bot.services.hack_import import (
    MAX_HTML_BYTES,
    PublicResolver,
    extract_page,
    extract_post,
    fetch_public_page,
    grounded_date,
    is_regulation,
    public_url,
    regulation_links,
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


def test_ostrovok_page_dates_share_the_explicit_event_year():
    # Relevant public page wording/layout, including dates without a repeated year.
    html = """<h1>О! Хакатон</h1><h2>Длительность</h2>
    <p>Восемь дней: с 23 октября по 30 октября 2026 года.</p>
    <h2>Как присоединиться</h2>
    <p>Регистрация закончится 16 октября в 23:59 (UTC+3)</p>
    <p>Разработчики, аналитики и продакты командой до пяти человек.</p>"""
    result = extract_page(html, "https://ostrovok.tech/hackathon/", MSK)
    assert result["registration_end"] == "2026-10-16T20:59:00+00:00"
    assert result["start_at"] == "2026-10-22T21:00:00+00:00"
    assert result["end_at"] == "2026-10-30T20:59:00+00:00"
    assert "team_min" not in result


def test_dates_without_year_are_not_anchored_to_current_or_ambiguous_year():
    for text in (
        "Хакатон\nРегистрация до 16 октября\nCopyright 2026",
        "Хакатон\n3–5 ноября 2025\n3–5 ноября 2026\nРегистрация до 16 октября",
        "Хакатон\nС 28 декабря по 3 января 2026\nРегистрация до 16 декабря",
    ):
        assert "registration_end" not in extract_post(text, MSK)


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


@pytest.mark.parametrize("mode", ["private_redirect", "oversized", "non_html", "pdf"])
async def test_fetch_rejects_unsafe_redirects_and_unbounded_or_non_page_content(monkeypatch, mode):
    requests = []

    class Response:
        status = 302 if mode == "private_redirect" else 200
        headers = {"Location": "http://127.0.0.1/secret"} if mode == "private_redirect" else {}
        content_type = (
            "image/png"
            if mode == "non_html"
            else "application/pdf"
            if mode == "pdf"
            else "text/html"
        )
        charset = "utf-8"

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            yield b"fake-pdf" if mode == "pdf" else b"a" * (MAX_HTML_BYTES + 1)

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
    if mode == "pdf":
        from unittest.mock import AsyncMock

        parser = AsyncMock(return_value="Хакатон <script>текст</script>")
        monkeypatch.setattr("bot.services.hack_import.pdf_text", parser)
        html, url = await fetch_public_page("https://hack.example/")
        assert "&lt;script&gt;" in html and url == "https://hack.example/"
        parser.assert_awaited_once_with(b"fake-pdf")
    else:
        with pytest.raises(UserError):
            await fetch_public_page("https://hack.example/")
    assert requests == ["https://hack.example/"]


@pytest.mark.parametrize("pdf", [False, True])
async def test_tilda_sized_html_is_read_but_pdf_limit_stays_one_mb(monkeypatch, pdf):
    from unittest.mock import AsyncMock

    # GlobalHack serves 1.08 MB of Tilda markup for a few thousand visible characters.
    page = (
        b"<style>"
        + b" " * 1_080_000
        + b"</style>"
        + "<h1>Хакатон</h1><p>13 — 16 ноября 2026</p>".encode()
    )

    class Response:
        status = 200
        content_type = "application/pdf" if pdf else "text/html"
        charset = "utf-8"

        @property
        def content(self):
            return self

        async def iter_chunked(self, size):
            for offset in range(0, len(page), size):
                yield page[offset : offset + size]

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

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("bot.services.hack_import.aiohttp.ClientSession", Session)
    parse_pdf = AsyncMock()
    monkeypatch.setattr("bot.services.hack_import.pdf_text", parse_pdf)
    if pdf:
        with pytest.raises(UserError, match="слишком большая"):
            await fetch_public_page("https://hack.example/")
    else:
        html, url = await fetch_public_page("https://hack.example/")
        data = extract_page(html, url, MSK)
        assert data["start_at"] == "2026-11-12T21:00:00+00:00"
        assert data["end_at"] == "2026-11-16T20:59:00+00:00"
    parse_pdf.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "quote", "source", "field", "expected"),
    [
        (
            "2026-10-19T20:59:00+00:00",
            "Регистрация закрыта",
            "Регистрация закрыта",
            "registration_end",
            None,
        ),
        (
            "2026-10-19T20:59:00+00:00",
            "Регистрация до 19 октября 2026",
            "Регистрация закрыта",
            "registration_end",
            None,
        ),
        (
            "2026-11-13T07:00:00+00:00",
            "13 — 16 ноября 2026",
            "Хакатон 13 — 16 ноября 2026",
            "start_at",
            "2026-11-12T21:00:00+00:00",
        ),
        (
            "2026-11-16T20:59:00+00:00",
            "13 — 16 ноября 2026",
            "Хакатон 13 — 16 ноября 2026",
            "end_at",
            "2026-11-16T20:59:00+00:00",
        ),
        (
            "2026-11-13T07:00:00+00:00",
            "Старт 13 ноября 2026 10:00",
            "Старт 13 ноября 2026 10:00",
            "start_at",
            "2026-11-13T07:00:00+00:00",
        ),
        (
            "2026-11-13T06:00:00+00:00",
            "Старт 13 ноября 2026 10:00",
            "Старт 13 ноября 2026 10:00",
            "start_at",
            None,
        ),
        (
            "2026-11-13T07:00:00+00:00",
            "2026-11-13T10:00:00+03:00",
            "2026-11-13T10:00:00+03:00",
            "start_at",
            "2026-11-13T07:00:00+00:00",
        ),
        (
            "2026-11-01T20:59:00+00:00",
            "Регистрация до 1 ноября",
            "Регистрация до 1 ноября. Copyright 2026",
            "registration_end",
            None,
        ),
        (
            "2026-11-01T20:59:00+00:00",
            "Регистрация до 1 ноября",
            "Регистрация до 1 ноября. Хакатон 13–16 ноября 2026",
            "registration_end",
            "2026-11-01T20:59:00+00:00",
        ),
    ],
)
def test_dates_require_real_quotes_and_supported_calendar_values(
    value, quote, source, field, expected
):
    assert grounded_date(value, quote, source, field, MSK) == expected


def test_rules_discovery_prefers_regulations_and_excludes_personal_data_documents():
    links = [
        {"url": "https://event.example/materials", "label": "Документы"},
        {"url": "https://event.example/privacy.pdf", "label": "Политика персональных данных"},
        {"url": "https://cloud.example/s/token", "label": "Положение"},
        {
            "url": "https://event.example/consent.pdf",
            "label": "Правила обработки персональных данных",
        },
        {"url": "https://event.example/regulations.pdf", "label": "Регламент"},
        {"url": "https://cloud.example/s/token/download", "label": "Скачать"},
        {"url": "https://event.example/register", "label": "Регистрация"},
    ]
    assert regulation_links(links) == [links[i]["url"] for i in (2, 4, 5, 0)]


@pytest.mark.parametrize(
    ("html", "url", "expected"),
    [
        ('<h1>Хакатон</h1><a href="/rules">Положение</a>', "https://event.example/", False),
        ("<h1>Хакатон</h1><h2>Общие положения</h2>", "https://event.example/", False),
        (
            '<h1>Документы</h1><p>Положение хакатона.pdf</p><a href="/download">Скачать</a>',
            "https://cloud.example/s/token",
            False,
        ),
        (
            "<h1>Положение о проведении хакатона</h1><p>1. Общие положения</p>",
            "https://event.example/page123.html",
            True,
        ),
        (
            "<pre>Положение о проведении хакатона. Общие положения</pre>",
            "https://cloud.example/s/token/download",
            True,
        ),
        (
            "<pre>Политика персональных данных хакатона. Общие положения</pre>",
            "https://cloud.example/s/policy/download",
            False,
        ),
    ],
)
def test_landing_pages_and_file_viewers_are_not_document_sources(html, url, expected):
    assert is_regulation(html, url) is expected


def test_embedded_pdf_and_accessible_icon_links_are_discovered():
    from bot.services.hack_llm import page_context

    context = page_context(
        '<h1>Хакатон</h1><a href="/opaque" aria-label="Положение"><img src="icon.png"></a>'
        '<iframe src="/regulations.pdf"></iframe><embed src="http://127.0.0.1/rules.pdf">',
        "https://event.example/",
    )
    assert regulation_links(context["links"]) == [
        "https://event.example/opaque",
        "https://event.example/regulations.pdf",
    ]


@pytest.mark.parametrize("first_year", [2024, 2026])
def test_legal_date_ranges_with_two_explicit_years_do_not_treat_2026_as_day_26(first_year):
    data = extract_post(
        f"Хакатон\nОсновной этап: с 23 октября {first_year} года по 30 октября 2026 года", MSK
    )
    assert data["start_at"] == f"{first_year}-10-22T21:00:00+00:00"
    assert data["end_at"] == "2026-10-30T20:59:00+00:00"


def test_pdf_date_spacing_and_typography_are_normalized_without_changing_facts():
    quote = "с 23 октября 2026 — 30 октября 2026"
    source = "Основной этап: с 23 октября2026-30октября2026."
    assert (
        grounded_date("2026-10-22T21:00:00+00:00", quote, source, "start_at", MSK)
        == "2026-10-22T21:00:00+00:00"
    )
    clock = "Начало 13 ноября 2026 в 10.00"
    assert (
        grounded_date("2026-11-13T07:00:00+00:00", clock, clock, "start_at", MSK)
        == "2026-11-13T07:00:00+00:00"
    )
