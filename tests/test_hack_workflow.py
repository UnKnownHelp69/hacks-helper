import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from bot.db.database import to_db
from bot.errors import UserError
from tests.conftest import ALLOWED_CHAT, USER_ID


async def draft(storage, *, source=None):
    now = datetime.now(UTC)
    fields = {
        "title": "Командный хакатон",
        "registration_end": to_db(now + timedelta(days=10)),
        "start_at": to_db(now + timedelta(days=11)),
        "end_at": to_db(now + timedelta(days=12)),
        "team_min": 2,
        "team_max": 3,
    }
    session = await storage.workflow.new_session(
        USER_ID, ALLOWED_CHAT, "preview", {"source_key": source}
    )
    return session, fields


async def test_publish_is_idempotent_and_source_is_deduplicated(storage):
    session, fields = await draft(storage, source="https://hack.example/")
    ids = await asyncio.gather(*(storage.workflow.publish_draft(session, fields) for _ in range(3)))
    assert len(set(ids)) == 1
    second, fields2 = await draft(storage, source="https://hack.example/")
    fields2["title"] = "Не перезаписывать существующую карточку"
    assert await storage.workflow.publish_draft(second, fields2) == ids[0]
    assert (await storage.hackathons.get_by_id(ids[0])).title == fields["title"]
    assert len(await storage.hackathons.list(ALLOWED_CHAT)) == 1


async def test_votes_form_team_once_without_admin_permissions(storage):
    session, fields = await draft(storage)
    hack_id = await storage.workflow.publish_draft(session, fields)
    await asyncio.gather(*(storage.workflow.vote(hack_id, user_id, "yes") for user_id in (1, 2, 3)))
    teams = await storage.workflow.teams(hack_id)
    assert len(teams) == 1 and teams[0].members == [1, 2]
    assert await storage.hackathons.members(hack_id) == [1, 2, 3]
    assert len(await storage.workflow.pending_notices()) == 1
    await asyncio.gather(*(storage.workflow.vote(hack_id, 2, "yes") for _ in range(3)))
    assert (await storage.workflow.teams(hack_id))[0].members == [1, 2]
    assert len(await storage.workflow.pending_notices()) == 1
    await storage.workflow.join_team(hack_id, 3, teams[0].id)
    await storage.workflow.vote(hack_id, 4, "yes")
    with pytest.raises(UserError, match="заполнена"):
        await storage.workflow.join_team(hack_id, 4, teams[0].id)
    await storage.workflow.join_team(hack_id, 4, teams[0].id, overflow=True)
    assert (await storage.workflow.teams(hack_id))[0].members == [1, 2, 3, 4]


async def test_new_team_recruits_and_switching_vote_removes_membership(storage):
    session, fields = await draft(storage)
    hack_id = await storage.workflow.publish_draft(session, fields)
    for user_id in (1, 2, 3):
        await storage.workflow.vote(hack_id, user_id, "yes")
    team_id = await storage.workflow.join_team(hack_id, 3, None)
    assert await storage.workflow.vote(hack_id, 4, "yes") == team_id
    assert len(await storage.workflow.pending_notices()) == 2
    await storage.workflow.vote(hack_id, 3, "no")
    teams = await storage.workflow.teams(hack_id)
    assert teams[1].members == [4]
    assert not await storage.hackathons.is_member(hack_id, 3)
    with pytest.raises(UserError, match="Участвую"):
        await storage.workflow.join_team(hack_id, 3, team_id)


async def test_cross_hackathon_team_and_expired_registration_are_rejected(storage):
    session, fields = await draft(storage)
    first = await storage.workflow.publish_draft(session, fields)
    session2, fields2 = await draft(storage)
    second = await storage.workflow.publish_draft(session2, fields2)
    team_id = await storage.workflow.vote(first, 1, "yes")
    await storage.workflow.vote(second, 1, "yes")
    with pytest.raises(UserError, match="недоступна"):
        await storage.workflow.join_team(second, 1, team_id)
    hack = await storage.hackathons.get_by_id(first)
    await storage.hackathons.update_dates(
        first, datetime.now(UTC) - timedelta(seconds=1), hack.start_at, hack.end_at
    )
    with pytest.raises(UserError, match="закончилась"):
        await storage.workflow.vote(first, 2, "yes")
    await storage.workflow.vote(first, 1, "no")


async def test_sessions_are_owner_bound_and_optimistic(storage):
    session, _ = await draft(storage)
    assert await storage.workflow.session(USER_ID + 1, session.token) is None
    stale = await storage.workflow.session(USER_ID, session.token)
    session.step = "input:title"
    await storage.workflow.save_session(session)
    with pytest.raises(UserError, match="уже обработано"):
        await storage.workflow.save_session(stale)


async def test_edit_keeps_checkpoint_ids_and_rejects_stale_card(storage):
    session, fields = await draft(storage)
    at = to_db(datetime.fromisoformat(fields["start_at"]) + timedelta(hours=1))
    session.data["checkpoints"] = [{"title": "Защита", "at": at}]
    await storage.workflow.save_session(session)
    hack_id = await storage.workflow.publish_draft(session, fields)
    checkpoint = (await storage.workflow.checkpoints(hack_id))[0]
    edit = await storage.workflow.new_session(
        USER_ID,
        ALLOWED_CHAT,
        "preview",
        {
            "editing_id": hack_id,
            "editing_revision": 0,
            "checkpoints": [{"title": "Защита", "at": at}],
        },
    )
    stale = await storage.workflow.new_session(USER_ID, ALLOWED_CHAT, "preview", dict(edit.data))
    fields["title"] = "Новое название"
    await storage.workflow.publish_draft(edit, fields)
    assert (await storage.workflow.checkpoints(hack_id))[0].id == checkpoint.id
    with pytest.raises(UserError, match="Карточка изменилась"):
        await storage.workflow.publish_draft(stale, fields)


async def test_publication_update_does_not_lose_a_concurrent_vote(storage):
    session, fields = await draft(storage)
    hack_id = await storage.workflow.publish_draft(session, fields)
    publication = (await storage.workflow.dirty_publications())[0]
    await storage.workflow.vote(hack_id, 1, "yes")
    await storage.workflow.publication_sent(hack_id, 101, publication["dirty"])
    publications = await storage.workflow.dirty_publications()
    assert len(publications) == 1 and publications[0]["message_id"] == 101


async def test_transaction_rolls_back_on_cancellation(storage):
    with pytest.raises(asyncio.CancelledError):
        async with storage.db.transaction() as conn:
            await conn.execute("INSERT INTO chats(chat_id,timezone) VALUES(99,'UTC')")
            raise asyncio.CancelledError
    assert await storage.db.fetchone("SELECT 1 FROM chats WHERE chat_id=99") is None
