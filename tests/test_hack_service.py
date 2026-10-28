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
    assert "Сайт недоступен" in session.data["import_error"]


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


ROOT = "https://event.example/"
RULES = ROOT + "rules"
RULES_TITLE = "<h1>Положение о проведении хакатона</h1>"


@pytest.fixture
def import_pages(monkeypatch):
    pages = {
        ROOT: "<h1>Хакатон из рекламы</h1><p>Команды от 80 до 90 человек</p>"
        '<p>Начало 05.11.2099</p><a href="/rules">Положение</a>',
        RULES: RULES_TITLE + "<p>Команды от 2 до 5 человек</p>",
    }

    async def fetch(url):
        value = pages.get(url, UserError("Документ недоступен"))
        if isinstance(value, UserError):
            raise value
        return value, url

    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", mock)
    return pages, mock


async def test_import_uses_rules_and_ignores_landing_and_advertising_post(
    service, storage, import_pages
):
    pages, fetch = import_pages
    pages[RULES] += "<p>Начало 05.11.2026 10:00</p><p>Конец 07.11.2026 18:00</p>"
    session = await service.import_post(
        ALLOWED_CHAT, USER_ID, "Хакатон из поста\nКоманда от 80 до 90\n" + ROOT
    )
    assert session.data["team_min"] == 2 and session.data["team_max"] == 5
    assert session.data["start_at"] == "2026-11-05T07:00:00+00:00"
    assert session.data["end_at"] == "2026-11-07T15:00:00+00:00"
    assert session.data["spec_url"] == RULES and session.data["site_url"] == ROOT
    assert session.data["import_sources"] == [RULES]
    assert session.data["regulations_import"]
    assert session.step == "preview"
    assert await storage.hackathons.list(ALLOWED_CHAT) == []
    assert [call.args[0] for call in fetch.await_args_list] == [ROOT, RULES]


async def test_no_rules_means_empty_draft_and_no_llm_fallback(service, import_pages, monkeypatch):
    pages, _ = import_pages
    pages[ROOT] = "<h1>Хакатон</h1><p>Начало 05.11.2099</p><p>Команда от 80 до 90</p>"
    service.llm.configured = True
    extract = AsyncMock()
    monkeypatch.setattr(service.llm, "extract", extract)
    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert session.data.get("start_at") is None and session.data.get("team_max") is None
    assert session.data["import_sources"] == []
    assert "положение / регламент" in session.data["import_error"]
    extract.assert_not_awaited()


async def test_llm_receives_only_rules_and_additional_regulations(
    service, import_pages, monkeypatch
):
    pages, fetch = import_pages
    other = ROOT + "reglament.pdf"
    pages[RULES] += (
        '<a href="/reglament.pdf">Регламент</a><a href="/privacy.pdf">Политика данных</a>'
    )
    pages[other] = "<pre>Регламент проведения хакатона. Дополнительные условия</pre>"
    service.llm.configured = True
    extract = AsyncMock(
        return_value={
            "is_hackathon": True,
            "title": "Название из положения",
            "team_min": 3,
            "team_max": 6,
            "follow_urls": ["https://invented.example/", "http://127.0.0.1/"],
        }
    )
    monkeypatch.setattr(service.llm, "extract", extract)
    session = await service.import_post(ALLOWED_CHAT, USER_ID, "Реклама: 2099\n" + ROOT)
    extract.assert_awaited_once()
    call = extract.await_args
    assert call.args[0] == "" and call.args[2] == RULES
    assert "2099" not in str(call)
    assert [item["url"] for item in call.kwargs["previous_pages"]] == [other]
    assert session.data["title"] == "Название из положения"
    assert session.data["import_sources"] == [RULES, other]
    assert session.data["site_url"] == ROOT
    assert [call.args[0] for call in fetch.await_args_list] == [ROOT, RULES, other]


async def test_public_document_viewer_is_followed_to_actual_download(service, import_pages):
    pages, fetch = import_pages
    viewer, pdf = "https://cloud.example/s/token", "https://cloud.example/s/token/download"
    pages[ROOT] = '<h1>Хакатон</h1><a href="' + viewer + '">Положение</a>'
    pages[viewer] = '<p>Положение.pdf</p><a href="/s/token/download">Скачать</a>'
    pages[pdf] = "<pre>Положение о проведении хакатона. Команда от 3 до 4 человек</pre>"
    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert session.data["import_sources"] == [pdf]
    assert session.data["spec_url"] == pdf and session.data["team_min"] == 3
    assert [call.args[0] for call in fetch.await_args_list] == [ROOT, viewer, pdf]


async def test_direct_rules_link_does_not_need_a_landing_page(service, import_pages):
    _, fetch = import_pages
    session = await service.import_post(ALLOWED_CHAT, USER_ID, RULES)
    assert session.data["team_min"] == 2 and session.data["import_sources"] == [RULES]
    fetch.assert_awaited_once_with(RULES)


async def test_llm_failure_preserves_document_parser_facts(service, import_pages, monkeypatch):
    pages, _ = import_pages
    pages[RULES] += "<p>Начало 05.11.2026 10:00</p>"
    service.llm.configured = True
    extract = AsyncMock(side_effect=UserError("LLM сбой"))
    monkeypatch.setattr(service.llm, "extract", extract)
    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert session.data["start_at"] == "2026-11-05T07:00:00+00:00"
    assert session.data["import_error"] == "LLM сбой"
    extract.assert_awaited_once()


async def test_personal_model_is_snapshotted_for_extraction_and_audit(
    service, storage, import_pages, monkeypatch
):
    pages, _ = import_pages
    pages[RULES] += "<p>Начало 05.11.2026 10:00</p>"
    service.llm.gigachat.configured = True
    service.llm.configured = True
    await storage.users.set_llm_preference(USER_ID, "gigachat", "GigaChat-Pro")
    selections = []

    async def extract(*args, selection, **kwargs):
        selections.append(selection)
        await storage.users.set_llm_preference(USER_ID, "openrouter", "another/model")
        return {"is_hackathon": True}

    monkeypatch.setattr(service.llm, "extract", extract)
    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert selections == [("gigachat", "GigaChat-Pro")] * 2
    assert session.data["llm_model"] == "GigaChat-Pro"
    assert await service.llm_selection(USER_ID) == ("openrouter", "another/model")
    assert await service.llm_selection(OTHER_ID) == ("openrouter", "openrouter/free")


async def test_model_selection_requires_membership_and_catalog_entry(service, storage, monkeypatch):
    monkeypatch.setattr(service.llm, "available_models", AsyncMock(return_value=["GigaChat"]))
    with pytest.raises(UserError, match="подтвердить"):
        await service.select_llm(ALLOWED_CHAT, OTHER_ID, "gigachat", "GigaChat")
    with pytest.raises(UserError, match="недоступна"):
        await service.select_llm(ALLOWED_CHAT, USER_ID, "gigachat", "invented")
    assert await storage.users.llm_preference(USER_ID) is None
    await service.select_llm(ALLOWED_CHAT, USER_ID, "gigachat", "GigaChat")
    assert await storage.users.llm_preference(USER_ID) == ("gigachat", "GigaChat")


@pytest.mark.parametrize("documents", [False, True])
async def test_discovery_and_document_limits_bound_cyclic_link_graph(
    service, monkeypatch, documents
):
    async def fetch(url):
        number = int(url.rsplit("/", 1)[1] or 0)
        heading = RULES_TITLE if documents and number else "<h1>Хакатон</h1>"
        return heading + f'<a href="/{number + 1}">Положение</a><a href="/">Документы</a>', url

    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch_mock)
    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert fetch_mock.await_count == (4 if documents else 6)
    assert len(session.data["import_sources"]) == (3 if documents else 0)


async def test_import_timeout_preserves_facts_from_rules(service, import_pages, monkeypatch):
    import asyncio

    pages, _ = import_pages
    pages[RULES] += "<p>Начало 05.11.2026 10:00</p>"
    service.llm.configured = True
    monkeypatch.setattr("bot.services.hackathons.IMPORT_TIMEOUT_SECONDS", 0.03)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(service.llm, "extract", slow)
    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert session.data["start_at"] == "2026-11-05T07:00:00+00:00"
    assert "две минуты" in session.data["import_error"]


async def test_unavailable_appendix_preserves_readable_rules(service, import_pages):
    pages, _ = import_pages
    pages[RULES] += '<a href="/reglament">Регламент</a>'
    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert session.data["team_min"] == 2 and session.data["import_sources"] == [RULES]
    assert "недоступен" in session.data["import_error"]


@pytest.mark.parametrize(
    "review",
    [
        {"registration_end": "2026-11-01T20:59:00+00:00"},
        {"registration_end": None},
        UserError("Сбой перепроверки"),
        {"registration_end": "2026-11-06T20:59:00+00:00"},
    ],
)
async def test_conflicting_dates_get_one_document_based_review(
    service, import_pages, monkeypatch, review
):
    pages, _ = import_pages
    pages[RULES] += "<p>Регистрация до 1 ноября 2026</p>"
    service.llm.configured = True
    extract = AsyncMock(
        side_effect=[
            {
                "is_hackathon": True,
                "registration_end": "2026-11-06T20:59:00+00:00",
                "start_at": "2026-11-05T07:00:00+00:00",
                "end_at": "2026-11-07T15:00:00+00:00",
            },
            review,
        ]
    )
    monkeypatch.setattr(service.llm, "extract", extract)
    stages = []

    async def progress(stage, data):
        stages.append(stage)

    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT, on_progress=progress)
    assert extract.await_count == 2
    audit = extract.await_args_list[1]
    assert audit.args[0] == "" and audit.args[2] is None
    assert [page["url"] for page in audit.kwargs["previous_pages"]] == [RULES]
    assert "Окончание регистрации" in audit.kwargs["review_issues"][0]
    assert any("перепроверяет даты" in stage for stage in stages)
    assert session.data["team_min"] == 2
    if isinstance(review, dict):
        assert session.data["registration_end"] == review["registration_end"]
        assert session.data["dates_reviewed"]
    else:
        assert session.data["registration_end"] == "2026-11-06T20:59:00+00:00"
    if not isinstance(review, dict) or review["registration_end"] != "2026-11-01T20:59:00+00:00":
        assert session.data["import_error"]


async def test_audit_uses_same_import_deadline(service, import_pages, monkeypatch):
    import asyncio

    service.llm.configured = True
    monkeypatch.setattr("bot.services.hackathons.IMPORT_TIMEOUT_SECONDS", 0.03)

    async def extract(*args, review_issues=None, **kwargs):
        if review_issues:
            await asyncio.sleep(1)
        return {
            "is_hackathon": True,
            "registration_end": "2026-11-06T20:59:00+00:00",
            "start_at": "2026-11-05T07:00:00+00:00",
        }

    monkeypatch.setattr(service.llm, "extract", extract)
    session = await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert session.data["start_at"] == "2026-11-05T07:00:00+00:00"
    assert "две минуты" in session.data["import_error"]


async def test_audit_does_not_drop_dates_late_in_the_document(service, import_pages, monkeypatch):
    pages, _ = import_pages
    pages[RULES] = RULES_TITLE + "<p>" + "x" * 13000 + "</p><p>Начало 05.11.2026 10:00</p>"
    service.llm.configured = True
    extract = AsyncMock(return_value={"is_hackathon": True})
    monkeypatch.setattr(service.llm, "extract", extract)
    await service.import_post(ALLOWED_CHAT, USER_ID, ROOT)
    assert extract.await_count == 2
    assert "05.11.2026" in extract.await_args.kwargs["previous_pages"][0]["text"]
