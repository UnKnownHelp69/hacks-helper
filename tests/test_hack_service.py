from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bot.errors import UserError
from bot.services.github import GitHubClient
from bot.services.hack_data import validate_hack
from bot.services.hackathons import HackathonService
from bot.services.users import UserDirectory
from tests.conftest import ALLOWED_CHAT, OTHER_ID, USER_ID, make_settings, user
from tests.test_hack_workflow import draft


@pytest.fixture
async def service(storage, fake_bot):
    github = GitHubClient(None)
    directory = UserDirectory(fake_bot, storage.users)
    service = HackathonService(make_settings(bot_admin_ids=frozenset()), storage, directory, github)
    fake_bot.set_member(ALLOWED_CHAT, user(USER_ID))
    try:
        yield service
    finally:
        await service.close()
        await github.close()


async def test_non_admin_member_can_publish_but_outsider_cannot(service, storage):
    session, fields = await draft(storage)
    session.data.update(fields)
    await storage.workflow.save_session(session)
    hack_id = await service.publish(session)
    assert (await service.hackathon(hack_id, USER_ID)).created_by == USER_ID
    with pytest.raises(UserError, match="подтвердить"):
        await service.hackathon(hack_id, OTHER_ID)
    with pytest.raises(UserError, match="подтвердить"):
        await service.vote(hack_id, OTHER_ID, "yes")


async def test_network_failure_does_not_grant_cached_membership(service, fake_bot):
    await service.require_access(ALLOWED_CHAT, USER_ID)
    fake_bot.network_down = True
    with pytest.raises(UserError, match="подтвердить"):
        await service.require_access(ALLOWED_CHAT, USER_ID)


async def test_member_leaving_group_loses_draft_access(service, storage, fake_bot):
    session, _ = await draft(storage)
    fake_bot.set_member(ALLOWED_CHAT, user(USER_ID), "left")
    with pytest.raises(UserError, match="подтвердить"):
        await service.session(USER_ID, session.token)


async def test_editing_existing_hack_does_not_depend_on_unchanged_github_repo(
    service, storage, monkeypatch
):
    from unittest.mock import AsyncMock

    session, fields = await draft(storage)
    session.data.update(fields, repo="team/project")
    await storage.workflow.save_session(session)
    hack_id = await service.publish(session)
    edit = await service.edit(hack_id, USER_ID)
    edit.data["title"] = "Новое название"
    await storage.workflow.save_session(edit)
    service.github.token = "test"
    get_repo = AsyncMock(side_effect=UserError("GitHub недоступен"))
    monkeypatch.setattr(service.github, "get_repo", get_repo)
    await service.publish(edit)
    assert (await storage.hackathons.get_by_id(hack_id)).title == "Новое название"
    get_repo.assert_not_awaited()


async def test_imported_post_is_a_persistent_owner_bound_draft(service, storage):
    session = await service.import_post(
        ALLOWED_CHAT, USER_ID, "Хакатон: начало 5 ноября 2026 в 10:00"
    )
    assert session.step == "preview" and session.data["start_at"].endswith("+00:00")
    assert (await storage.workflow.session(USER_ID, session.token)).data == session.data
    assert await storage.workflow.session(OTHER_ID, session.token) is None


async def test_explicit_import_keeps_a_draft_when_site_cannot_be_read(service, monkeypatch):
    async def unavailable(url):
        raise UserError("Сайт недоступен")

    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", unavailable)
    session = await service.import_post(
        ALLOWED_CHAT, USER_ID, "https://event.example/", explicit=True
    )
    assert session.data["site_url"] == "https://event.example/"
    assert session.data["import_error"] == "Сайт недоступен"


def test_validation_rejects_missing_dates_and_invalid_team_bounds():
    with pytest.raises(UserError, match="Заполните"):
        validate_hack({"title": "Без дат"})
    now = datetime.now(UTC)
    data = {
        "title": "Хакатон",
        "registration_end": now.isoformat(),
        "start_at": (now + timedelta(days=1)).isoformat(),
        "end_at": (now + timedelta(days=2)).isoformat(),
        "team_min": 5,
        "team_max": 2,
    }
    with pytest.raises(UserError, match="Размер команды"):
        validate_hack(data)
    data.update(team_min=2, team_max=4)
    data["checkpoints"] = [{"title": "Слишком поздно", "at": (now + timedelta(days=3)).isoformat()}]
    with pytest.raises(UserError, match="Чекпоинт"):
        validate_hack(data)


async def test_llm_import_preserves_post_and_parser_facts_and_stays_a_draft(
    service,
    storage,
    monkeypatch,
):
    monkeypatch.setattr(
        "bot.services.hackathons.fetch_public_page",
        AsyncMock(
            return_value=("<p>Хакатон</p><p>Конец 10.11.2026 18:00</p>", "https://event.example/")
        ),
    )
    service.llm.configured = True
    extract = AsyncMock(
        return_value={
            "is_hackathon": True,
            "title": "LLM title",
            "team_min": 3,
            "team_max": 6,
            "start_at": "2026-11-05T07:00:00+00:00",
            "checkpoints": [],
        }
    )
    monkeypatch.setattr(service.llm, "extract", extract)
    session = await service.import_post(
        ALLOWED_CHAT,
        USER_ID,
        "Хакатон команды\nКоманда от 2 до 4\nhttps://event.example/",
    )
    assert session.data["title"] == "Хакатон команды"
    assert (session.data["team_min"], session.data["team_max"]) == (2, 4)
    assert session.data["end_at"] == "2026-11-10T15:00:00+00:00"
    assert session.data["llm_imported"]
    assert session.step == "preview"
    assert await storage.hackathons.list(ALLOWED_CHAT) == []
    assert await storage.workflow.session(USER_ID, session.token)
    extract.assert_awaited_once()


async def test_llm_failure_preserves_deterministic_data(service, monkeypatch):
    service.llm.configured = True
    monkeypatch.setattr(service.llm, "extract", AsyncMock(side_effect=UserError("LLM сбой")))
    session = await service.import_post(ALLOWED_CHAT, USER_ID, "Хакатон: начало 05.11.2026 10:00")
    assert session.data["start_at"] == "2026-11-05T07:00:00+00:00"
    assert session.data["import_error"] == "LLM сбой"


async def test_llm_can_recognize_a_link_without_regex_keyword(service, monkeypatch):
    monkeypatch.setattr(
        "bot.services.hackathons.fetch_public_page",
        AsyncMock(return_value=("<h1>AI Challenge</h1>", "https://event.example/")),
    )
    service.llm.configured = True
    monkeypatch.setattr(
        service.llm,
        "extract",
        AsyncMock(
            return_value={
                "is_hackathon": True,
                "title": "AI Challenge",
                "checkpoints": [],
            }
        ),
    )
    session = await service.import_post(ALLOWED_CHAT, USER_ID, "https://event.example/")
    assert session.data["title"] == "AI Challenge"


async def test_research_follows_only_discovered_links_and_reports_incremental_fields(
    service,
    monkeypatch,
):
    root, rules = "https://event.example/", "https://event.example/rules.pdf"
    pages = {
        root: '<h1>Хакатон</h1><a href="/rules.pdf">Правила</a>',
        rules: "<pre>Команды от 2 до 5 человек</pre>",
    }
    fetch = AsyncMock(side_effect=lambda url: (pages[url], url))
    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch)
    service.llm.configured = True
    extract = AsyncMock(
        side_effect=[
            {
                "title": "Название из LLM",
                "team_max": 5,
                "is_hackathon": True,
                "checkpoints": [],
                "follow_urls": [rules, "https://invented.example/", "http://127.0.0.1/"],
            },
            {"team_min": 2, "team_max": 5, "checkpoints": [], "follow_urls": [root]},
        ]
    )
    monkeypatch.setattr(service.llm, "extract", extract)
    updates = []

    async def progress(stage, data):
        updates.append((stage, dict(data)))

    session = await service.import_post(ALLOWED_CHAT, USER_ID, root, on_progress=progress)
    assert [call.args[0] for call in fetch.await_args_list] == [root, rules]
    assert session.data["team_min"] == 2 and session.data["team_max"] == 5
    assert session.data["title"] == "Название из LLM"
    assert session.data["site_url"] == root and session.data["import_sources"] == [root, rules]
    assert "follow_urls" not in session.data
    assert any(data.get("team_max") == 5 and not data.get("team_min") for _, data in updates)
    assert updates[-1][1]["team_min"] == 2
    assert extract.await_args_list[1].kwargs["previous_pages"][0]["url"] == root
    assert extract.await_args_list[1].kwargs["current"]["team_max"] == 5


async def test_research_has_a_document_limit_even_when_pages_link_to_more(service, monkeypatch):
    root = "https://event.example/"

    async def fetch(url):
        number = int(url.rsplit("/", 1)[1] or 0)
        return f'<h1>Хакатон</h1><a href="/{number + 1}">Правила</a>', url

    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch_mock)
    service.llm.configured = True

    async def extract(post, html, url, tz, **kwargs):
        number = int(url.rsplit("/", 1)[1] or 0)
        return {"is_hackathon": True, "follow_urls": [root + str(number + 1)]}

    monkeypatch.setattr(service.llm, "extract", AsyncMock(side_effect=extract))
    session = await service.import_post(ALLOWED_CHAT, USER_ID, root)
    assert fetch_mock.await_count == 4
    assert len(session.data["import_sources"]) == 4


async def test_research_timeout_preserves_already_extracted_facts(service, monkeypatch):
    import asyncio

    monkeypatch.setattr("bot.services.hackathons.IMPORT_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(
        "bot.services.hackathons.fetch_public_page",
        AsyncMock(
            return_value=(
                "<h1>Хакатон</h1><p>Начало 5 ноября 2026 10:00</p>",
                "https://event.example/",
            )
        ),
    )
    service.llm.configured = True

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(service.llm, "extract", slow)
    session = await service.import_post(ALLOWED_CHAT, USER_ID, "https://event.example/")
    assert session.data["start_at"] == "2026-11-05T07:00:00+00:00"
    assert "две минуты" in session.data["import_error"]
    assert session.step == "preview"


async def test_failed_followup_keeps_successful_llm_result(service, monkeypatch):
    root, rules = "https://event.example/", "https://event.example/rules"
    monkeypatch.setattr(
        "bot.services.hackathons.fetch_public_page",
        AsyncMock(
            side_effect=[
                ('<h1>Хакатон</h1><a href="/rules">Правила</a>', root),
                UserError("Не читается"),
            ]
        ),
    )
    service.llm.configured = True
    monkeypatch.setattr(
        service.llm,
        "extract",
        AsyncMock(
            return_value={
                "team_min": 2,
                "team_max": 5,
                "is_hackathon": True,
                "follow_urls": [rules],
            }
        ),
    )
    session = await service.import_post(ALLOWED_CHAT, USER_ID, root)
    assert session.data["team_min"] == 2 and session.data["llm_imported"]
    assert "связанных документов" in session.data["import_error"]
