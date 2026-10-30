"""Task sync, reminders and archive with an in-memory DB and fake Telegram/GitHub."""

from datetime import UTC, datetime, timedelta

import pytest

from bot.db.models import (
    HACK_FINISHED,
    MILESTONE_START,
    TASK_CLOSED,
    TASK_IN_PROGRESS,
    TASK_NOT_STARTED,
)
from bot.errors import UserError
from bot.services.archive import ArchiveService
from bot.services.availability import parse_day_value, serialize_day
from bot.services.github import GitHubNotFound, Issue
from bot.services.notify import Messenger, Pinger
from bot.services.reminders import ReminderService
from bot.services.tasks import TaskService
from bot.services.users import UserDirectory
from tests.conftest import ALLOWED_CHAT, FOREIGN_CHAT, make_settings, user

REPO = "team/project"
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)  # Thursday 12:00 MSK
ALICE, BOB = 11111, 22222


class FakeGitHub:
    configured = True

    def __init__(self) -> None:
        self.issues: dict[int, Issue] = {}
        self.linked_prs: set[int] = set()
        self.added: list[tuple[int, list[str]]] = []
        self.removed: list[tuple[int, list[str]]] = []

    def put(self, number: int, state: str = "open", assignees=(), labels=(), title=None):
        self.issues[number] = Issue(
            number,
            title or f"Issue {number}",
            state,
            f"https://github.com/{REPO}/issues/{number}",
            tuple(assignees),
            tuple(labels),
        )

    async def get_issue(self, repo, number):
        if number not in self.issues:
            raise GitHubNotFound("not found")
        return self.issues[number]

    async def list_open_issues(self, repo, **kw):
        return [i for i in self.issues.values() if i.state == "open"]

    async def has_open_linked_pr(self, repo, number):
        return number in self.linked_prs

    async def add_assignees(self, repo, number, logins):
        self.added.append((number, logins))
        issue = self.issues[number]
        self.put(number, issue.state, (*issue.assignees, *logins), issue.labels, issue.title)
        return self.issues[number]

    async def remove_assignees(self, repo, number, logins):
        self.removed.append((number, logins))
        issue = self.issues[number]
        rest = [a for a in issue.assignees if a not in logins]
        self.put(number, issue.state, rest, issue.labels, issue.title)
        return self.issues[number]


@pytest.fixture
async def env(storage, fake_bot):
    settings = make_settings(reminder_offsets_minutes=[1440, 180, 60])
    github = FakeGitHub()
    directory = UserDirectory(fake_bot, storage.users)
    pinger = Pinger(directory, storage.availability)
    messenger = Messenger(fake_bot, settings)
    tasks = TaskService(settings, storage, github, pinger, messenger)  # type: ignore[arg-type]
    reminders = ReminderService(settings, storage, pinger, messenger)
    archive = ArchiveService(settings, storage, tasks)
    for uid, name in ((ALICE, "alice_tg"), (BOB, "bob_tg")):
        fake_bot.set_member(ALLOWED_CHAT, user(uid, name))
    hack = await storage.hackathons.create(
        ALLOWED_CHAT,
        "hack",
        None,
        NOW - timedelta(days=2),
        NOW - timedelta(hours=1),
        NOW + timedelta(days=1),
    )
    await storage.hackathons.update_fields(hack.id, repo=REPO)
    await storage.hackathons.add_member(hack.id, ALICE)
    await storage.hackathons.add_member(hack.id, BOB)
    await storage.github_links.set_login(ALICE, "alice-gh")
    hack = await storage.hackathons.get_by_id(hack.id)
    return {
        "settings": settings,
        "github": github,
        "tasks": tasks,
        "reminders": reminders,
        "archive": archive,
        "hack": hack,
        "storage": storage,
        "bot": fake_bot,
    }


# --- assignment ---


async def test_assign_sets_github_assignee_when_login_linked(env):
    env["github"].put(12, title="Авторизация")
    result = await env["tasks"].assign(env["hack"], ALICE, 12)
    assert result.task.status == TASK_NOT_STARTED and result.task.seen_assigned
    assert env["github"].added == [(12, ["alice-gh"])]
    assert result.warnings == []


async def test_assign_without_login_warns_and_keeps_task(env):
    env["github"].put(12)
    result = await env["tasks"].assign(env["hack"], BOB, 12)
    assert result.task is not None and not result.task.seen_assigned
    assert env["github"].added == []
    assert "GitHub не привязан" in result.warnings[0]


async def test_assign_validations(env):
    env["github"].put(1, state="closed")
    with pytest.raises(UserError, match="не найден"):
        await env["tasks"].assign(env["hack"], ALICE, 99)
    with pytest.raises(UserError, match="уже закрыт"):
        await env["tasks"].assign(env["hack"], ALICE, 1)
    env["github"].put(2)
    with pytest.raises(UserError, match="участвует"):
        await env["tasks"].assign(env["hack"], 999999, 2)


async def test_unassign_removes_github_assignee(env):
    env["github"].put(12)
    await env["tasks"].assign(env["hack"], ALICE, 12)
    await env["tasks"].unassign(env["hack"], ALICE, 12)
    assert env["github"].removed == [(12, ["alice-gh"])]
    assert await env["storage"].tasks.active_for_hackathon(env["hack"].id) == []


# --- periodic sync ---


async def test_sync_picks_up_github_assignee(env):
    env["github"].put(5, assignees=["Alice-GH"], labels=["in progress"])
    lines = await env["tasks"].sync_hackathon(env["hack"], NOW)
    tasks = await env["storage"].tasks.active_for_hackathon(env["hack"].id)
    assert [(t.user_id, t.issue_number, t.status) for t in tasks] == [(ALICE, 5, TASK_IN_PROGRESS)]
    assert "из GitHub" in lines[0] and "@alice_tg" in lines[0]
    # second sync: nothing new
    assert await env["tasks"].sync_hackathon(env["hack"], NOW) == []


async def test_sync_status_changes_and_close(env):
    env["github"].put(7)
    await env["tasks"].assign(env["hack"], ALICE, 7)
    env["github"].linked_prs.add(7)
    lines = await env["tasks"].sync_hackathon(env["hack"], NOW)
    assert lines and "в работе" in lines[0]
    env["github"].put(7, state="closed", assignees=["alice-gh"])
    lines = await env["tasks"].sync_hackathon(env["hack"], NOW)
    assert lines and "закрыт ✅" in lines[0]
    assert await env["storage"].tasks.active_for_hackathon(env["hack"].id) == []
    assert (await env["storage"].tasks.closed_counts(env["hack"].id)) == (1, {ALICE: 1})


async def test_sync_removes_task_when_assignee_removed_in_github(env):
    env["github"].put(8, assignees=["alice-gh"])
    await env["tasks"].sync_hackathon(env["hack"], NOW)
    env["github"].put(8, assignees=[])
    lines = await env["tasks"].sync_hackathon(env["hack"], NOW)
    assert "assignee снят" in lines[0]
    assert "@alice_tg" not in lines[0]  # removal notice does not ping
    assert await env["storage"].tasks.active_for_hackathon(env["hack"].id) == []


async def test_sync_keeps_bot_task_never_assigned_in_github(env):
    env["github"].put(9)
    await env["tasks"].assign(env["hack"], BOB, 9)  # no login -> no GitHub assignee
    await env["storage"].github_links.set_login(BOB, "bob-gh")  # linked later
    assert await env["tasks"].sync_hackathon(env["hack"], NOW) == []
    assert len(await env["storage"].tasks.active_for_hackathon(env["hack"].id)) == 1


async def test_sync_ignores_non_members(env):
    await env["storage"].github_links.set_login(33333, "stranger")
    env["github"].put(4, assignees=["stranger"])
    assert await env["tasks"].sync_hackathon(env["hack"], NOW) == []


async def test_notification_does_not_ping_unavailable_user(env):
    # Thursday is "never" for Alice -> her name is shown without @
    await env["storage"].availability.set_days(ALICE, {3: serialize_day(parse_day_value("нет"))})
    env["github"].put(5, assignees=["alice-gh"])
    lines = await env["tasks"].sync_hackathon(env["hack"], NOW)
    assert "alice_tg" in lines[0] and "@alice_tg" not in lines[0]


async def test_poll_skips_hackathons_of_foreign_chats(env, storage):
    other = await storage.hackathons.create(
        FOREIGN_CHAT, "foreign", None, NOW, NOW, NOW + timedelta(days=1)
    )
    await storage.hackathons.update_fields(other.id, repo=REPO)
    env["github"].put(5, assignees=["alice-gh"])
    await storage.hackathons.add_member(other.id, ALICE)
    await env["tasks"].poll_all()
    assert await storage.tasks.active_for_hackathon(other.id) == []
    assert all(chat == ALLOWED_CHAT for chat, _ in env["bot"].sent)


# --- reminders ---


async def _start_in(env, delta: timedelta) -> None:
    hack = env["hack"]
    start = NOW + delta
    await env["storage"].hackathons.update_dates(
        hack.id, start - timedelta(days=1), start, start + timedelta(days=1)
    )


async def test_reminder_sent_once_and_survives_restart(env):
    await _start_in(env, timedelta(hours=2, minutes=30))
    await env["reminders"].send_due_reminders(NOW)
    sent = [text for _, text in env["bot"].sent if "до начала" in text]
    assert len(sent) == 1  # 24h and 3h offsets due at once -> single message
    assert "@alice_tg" in sent[0] and "@bob_tg" in sent[0]
    # "restart": a fresh service instance sees the stored marks
    fresh = ReminderService(
        env["settings"], env["storage"], env["reminders"].pinger, env["reminders"].messenger
    )
    await fresh.send_due_reminders(NOW + timedelta(minutes=1))
    assert len([t for _, t in env["bot"].sent if "до начала" in t]) == 1
    # the 1h reminder comes later
    await fresh.send_due_reminders(NOW + timedelta(hours=1, minutes=31))
    assert len([t for _, t in env["bot"].sent if "до начала" in t]) == 2


async def test_reminders_recalculated_after_date_change(env):
    await _start_in(env, timedelta(hours=2))
    await env["reminders"].send_due_reminders(NOW)
    count = len(env["bot"].sent)
    await _start_in(env, timedelta(hours=20))  # moved later: 24h reminder is due again
    await env["reminders"].send_due_reminders(NOW)
    assert len(env["bot"].sent) == count + 1


async def test_reminder_skips_left_member_and_defers_unavailable(env):
    storage, bot = env["storage"], env["bot"]
    bot.set_member(ALLOWED_CHAT, user(BOB, "bob_tg"), "left")
    # Alice: Thursday only 15:00-16:00 MSK (now is 12:00 MSK)
    await storage.availability.set_days(ALICE, {3: serialize_day(parse_day_value("15:00-16:00"))})
    await _start_in(env, timedelta(hours=5))
    await env["reminders"].send_due_reminders(NOW)
    text = bot.sent[-1][1]
    assert "@alice_tg" not in text and "@bob_tg" not in text
    assert "bob_tg нет в группе" in text
    assert "⏸ Позже: alice_tg" in text
    deferred = await storage.reminders.list_deferred()
    assert [(d.user_id, d.milestone) for d in deferred] == [(ALICE, MILESTONE_START)]

    await env["reminders"].process_deferred(NOW + timedelta(hours=1))  # 13:00, still not
    assert len(await storage.reminders.list_deferred()) == 1
    await env["reminders"].process_deferred(NOW + timedelta(hours=3, minutes=1))  # 15:01
    assert "@alice_tg" in bot.sent[-1][1] and "до начала" in bot.sent[-1][1]
    assert await storage.reminders.list_deferred() == []


async def test_deferred_ping_dropped_if_date_changed(env):
    storage = env["storage"]
    await storage.availability.set_days(ALICE, {3: serialize_day(parse_day_value("15:00-16:00"))})
    await _start_in(env, timedelta(hours=5))
    await env["reminders"].send_due_reminders(NOW)
    await _start_in(env, timedelta(hours=10))
    sent = len(env["bot"].sent)
    await env["reminders"].process_deferred(NOW + timedelta(hours=3, minutes=1))
    assert await storage.reminders.list_deferred() == []
    assert len(env["bot"].sent) == sent


# --- archive ---


async def test_archive_after_end(env):
    storage = env["storage"]
    env["github"].put(1)
    env["github"].put(2)
    await env["tasks"].assign(env["hack"], ALICE, 1)
    await env["tasks"].assign(env["hack"], BOB, 2)
    env["github"].put(1, state="closed", assignees=["alice-gh"])  # closed right before the end

    assert await env["archive"].archive_due(NOW) == []
    archived = await env["archive"].archive_due(env["hack"].end_at)
    assert archived == [env["hack"].id]
    hack = await storage.hackathons.get_by_id(env["hack"].id)
    assert hack.status == HACK_FINISHED
    assert await storage.tasks.active_for_hackathon(hack.id) == []
    assert await storage.tasks.closed_counts(hack.id) == (1, {ALICE: 1})
    notices = await storage.workflow.pending_notices()
    assert "завершён, закрыто issues: 1" in notices[0]["text"]
    assert await storage.hackathons.members(hack.id) == [ALICE, BOB]
    # not archived twice
    assert await env["archive"].archive_due(env["hack"].end_at + timedelta(hours=1)) == []
    task = await storage.tasks.get(hack.id, ALICE, 1)
    assert task.status == TASK_CLOSED


async def test_archive_announcement_is_queued_atomically(env):
    await env["archive"].archive_due(env["hack"].end_at)
    notices = await env["storage"].workflow.pending_notices()
    assert len(notices) == 1 and notices[0]["event_key"] == f"archive:{env['hack'].id}"


async def test_checkpoint_reminders_and_actual_event_are_sent_once(env):
    from bot.db.database import to_db

    storage, reminders = env["storage"], env["reminders"]
    at = NOW + timedelta(hours=5)
    await storage.db.execute(
        "INSERT INTO hack_checkpoints(hackathon_id,title,at) VALUES(?,?,?)",
        (env["hack"].id, "Промежуточная защита", to_db(at)),
    )
    await reminders.send_due_reminders(NOW)
    await reminders.send_due_reminders(NOW)
    assert len([text for _, text in env["bot"].sent if "Промежуточная защита" in text]) == 1
    await reminders.tick(at)
    count = len(env["bot"].sent)
    assert len([text for _, text in env["bot"].sent if "Промежуточная защита" in text]) == 2
    await reminders.tick(at)
    assert len(env["bot"].sent) == count


async def test_failed_reminder_is_not_marked_sent(env, monkeypatch):
    from unittest.mock import AsyncMock

    await _start_in(env, timedelta(hours=2))
    send = AsyncMock(return_value=False)
    monkeypatch.setattr(env["reminders"].messenger, "send", send)
    await env["reminders"].send_due_reminders(NOW)
    hack = await env["storage"].hackathons.get_by_id(env["hack"].id)
    assert (
        await env["storage"].reminders.sent_offsets(hack.id, MILESTONE_START, hack.start_at)
        == set()
    )
    send.return_value = True
    await env["reminders"].send_due_reminders(NOW)
    assert await env["storage"].reminders.sent_offsets(hack.id, MILESTONE_START, hack.start_at) == {
        1440,
        180,
    }


async def test_simultaneous_assignments_create_one_task_and_one_personal_notice(env, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from bot.services.tasks import TaskResult

    env["github"].put(12)
    sender = AsyncMock(wraps=env["bot"].send_message)
    monkeypatch.setattr(env["bot"], "send_message", sender)
    results = await asyncio.gather(
        *(env["tasks"].assign(env["hack"], ALICE, 12) for _ in range(2)),
        return_exceptions=True,
    )
    assert len([r for r in results if isinstance(r, TaskResult)]) == 1
    assert len([r for r in results if isinstance(r, UserError)]) == 1
    assert env["github"].added == [(12, ["alice-gh"])]
    assert len(await env["storage"].tasks.active_for_hackathon(env["hack"].id)) == 1
    sender.assert_awaited_once()
    assert sender.await_args.kwargs["ephemeral_message_parameters"].receiver_user_id == ALICE


async def test_personal_task_notice_cannot_be_sent_to_someone_elses_private_chat(env):
    sender = env["tasks"].messenger
    before = len(env["bot"].sent)
    assert not await sender.send(ALICE, "Личная задача Bob", receiver_user_id=BOB)
    assert len(env["bot"].sent) == before


@pytest.mark.parametrize("chat_id", [ALICE, -99999])
async def test_task_service_cannot_modify_github_for_unconfigured_chats(env, chat_id):
    from dataclasses import replace

    hack = env["hack"]
    await env["storage"].db.execute(
        "UPDATE hackathons SET chat_id=? WHERE id=?", (chat_id, hack.id)
    )
    hack = replace(hack, chat_id=chat_id)
    env["github"].put(12)
    with pytest.raises(UserError, match="чат не подключён"):
        await env["tasks"].assign(hack, ALICE, 12)
    with pytest.raises(UserError, match="чат не подключён"):
        await env["tasks"].create_issue(hack, "Issue", None, ALICE)
    with pytest.raises(UserError, match="чат не подключён"):
        await env["tasks"].unassign(hack, ALICE, 12)
    assert await env["tasks"].sync_hackathon(hack) == []
    assert not await env["tasks"].messenger.send(chat_id, "Личная задача", receiver_user_id=ALICE)
    assert not await env["tasks"].messenger.send(chat_id, "Общее сообщение")
    assert env["github"].added == [] and env["github"].removed == []
    assert await env["storage"].tasks.active_for_hackathon(hack.id) == []
