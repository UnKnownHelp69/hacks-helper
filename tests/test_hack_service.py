from datetime import UTC, datetime, timedelta

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
