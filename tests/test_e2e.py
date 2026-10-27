"""End-to-end: real Dispatcher + handlers, mocked Telegram session and GitHub API."""

from __future__ import annotations

import importlib
import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from html import unescape
from typing import Any

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    GetChatMember,
    GetMe,
    LeaveChat,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberMember,
    Document,
    Message,
    MessageEntity,
    Update,
    User,
)

import bot.handlers
from bot import app as app_module
from bot.app import build_context
from bot.handlers import admin, availability, common, events, github, hackathons, popups, tasks
from bot.services.github import GitHubClient
from tests.conftest import ADMIN_ID, ALLOWED_CHAT, FOREIGN_CHAT, USER_ID, make_settings

BOT_ID = 4242
REPO = "team/project"
_ids = itertools.count(1)


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.members: dict[int, User] = {}

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None):
        self.requests.append(method)
        now = datetime(2026, 10, 26, tzinfo=UTC)
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(
                message_id=next(_ids),
                date=now,
                chat=Chat(
                    id=method.chat_id, type="private" if method.chat_id > 0 else "supergroup"
                ),
                text=getattr(method, "text", None),
            )
        if isinstance(method, GetChatMember):
            if method.user_id in self.members:
                return ChatMemberMember(user=self.members[method.user_id])
            raise TelegramBadRequest(method=method, message="Bad Request: user not found")
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="hacks_bot")
        return True

    def texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, SendMessage)]

    def last_text(self) -> str:
        return self.texts()[-1]


def github_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/repos/{REPO}":
        return httpx.Response(200, json={"full_name": REPO})
    if path.startswith("/users/"):
        return httpx.Response(200, json={"login": path.rsplit("/", 1)[-1]})
    issue = {
        "number": 12,
        "title": "Авторизация",
        "state": "open",
        "html_url": f"https://github.com/{REPO}/issues/12",
        "assignees": [],
        "labels": [],
    }
    if path == f"/repos/{REPO}/issues" and request.method == "GET":
        return httpx.Response(200, json=[issue])
    if path == f"/repos/{REPO}/issues" and request.method == "POST":
        return httpx.Response(201, json={**issue, "number": 13, "title": "Новая"})
    if path.endswith("/assignees"):
        return httpx.Response(201, json={**issue, "assignees": [{"login": "alice-gh"}]})
    if path.endswith("/timeline"):
        return httpx.Response(200, json=[])
    if path.startswith(f"/repos/{REPO}/issues/"):
        return httpx.Response(200, json=issue)
    return httpx.Response(404, json={"message": "Not Found"})


def build_dispatcher(ctx):
    """aiogram routers can be attached only once: reload handler modules for fresh routers."""
    for module in (admin, availability, common, events, github, hackathons, popups, tasks):
        importlib.reload(module)
    importlib.reload(bot.handlers)
    importlib.reload(app_module)
    return app_module.build_dispatcher(ctx)


@pytest.fixture
async def app(storage):
    settings = make_settings()
    session = MockSession()
    bot = Bot("123:TEST", session=session)
    github = GitHubClient("test-token", transport=httpx.MockTransport(github_handler))
    ctx = build_context(settings, bot, storage, github)
    dp = build_dispatcher(ctx)
    yield {"bot": bot, "dp": dp, "session": session, "ctx": ctx, "storage": storage}
    await github.close()
    # no handler may crash: unexpected exceptions produce this message
    assert not any("Внутренняя ошибка" in t for t in session.texts())


def tg_user(user_id: int, username: str | None, name: str = "User") -> User:
    return User(id=user_id, is_bot=False, first_name=name, username=username)


ADMIN = tg_user(ADMIN_ID, "admin_user", "Admin")
ALICE = tg_user(USER_ID, "alice_tg", "Alice")


async def send(app, text: str, sender: User = ADMIN, chat_id: int = ALLOWED_CHAT, **kw) -> None:
    app["session"].members.setdefault(sender.id, sender)
    command = text.split()[0] if text.startswith("/") else None
    entities = kw.pop("entities", None)
    if command and entities is None:
        entities = [MessageEntity(type="bot_command", offset=0, length=len(command))]
    chat_type = "private" if chat_id > 0 else "supergroup"
    message = Message(
        message_id=next(_ids),
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=chat_id, type=chat_type, title=None if chat_id > 0 else "Team"),
        from_user=sender,
        text=text,
        entities=entities,
        **kw,
    )
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), message=message))


async def shown(app, presser: User) -> str:
    """What ``presser`` sees for the last answer: the popup text or the chat message."""
    session = app["session"]
    last = [m for m in session.requests if isinstance(m, SendMessage)][-1]
    markup = last.reply_markup
    data = markup.inline_keyboard[0][0].callback_data if markup else None
    if not data or not data.startswith(("pop:", "my:")):
        return last.text
    message = Message(
        message_id=next(_ids),
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=last.chat_id, type="private" if last.chat_id > 0 else "supergroup"),
        text=last.text,
    )
    callback = CallbackQuery(
        id=str(next(_ids)), from_user=presser, chat_instance="x", data=data, message=message
    )
    before = len(session.requests)
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), callback_query=callback))
    new = session.requests[before:]
    for method in new:
        if isinstance(method, AnswerCallbackQuery) and method.show_alert:
            return method.text
    # too long for a popup: posted to the chat instead
    return "\n".join(m.text for m in new if isinstance(m, SendMessage))


async def create_hack(app, chat_id=ALLOWED_CHAT, slug="h2026", title="Хакатон"):
    return await app["storage"].hackathons.create(
        chat_id,
        slug,
        title,
        datetime(2026, 12, 1, 20, 59, tzinfo=UTC),
        datetime(2026, 12, 5, 7, 0, tzinfo=UTC),
        datetime(2026, 12, 7, 15, 0, tzinfo=UTC),
    )


async def press(app, data, presser=ADMIN, chat_id=ALLOWED_CHAT):
    message = Message(
        message_id=next(_ids),
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
        text="👀",
    )
    callback = CallbackQuery(
        id=str(next(_ids)), from_user=presser, chat_instance="x", data=data, message=message
    )
    before = len(app["session"].requests)
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), callback_query=callback))
    return app["session"].requests[before:]


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT, ADMIN_ID])
async def test_four_public_commands(app, chat_id):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    await send(app, "/help@hacks_bot", chat_id=chat_id)
    help_text = app["session"].last_text()
    for name in ("help", "hack", "me", "github"):
        assert f"/{name}" in help_text
    assert "/hack_new" not in help_text and "/link_github" not in help_text
    await send(app, "/me", chat_id=chat_id)
    assert f"id: {ADMIN_ID}" in await shown(app, ADMIN)
    await send(app, "/hack", chat_id=chat_id)
    assert await shown(app, ADMIN) == "Активных хакатонов нет"
    await send(app, "/github", chat_id=chat_id)
    assert "Аккаунт: не привязан" in app["session"].last_text()
    assert "Интеграция: настроена" in app["session"].last_text()


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT, ADMIN_ID])
async def test_hack_list_and_card_are_chat_scoped(app, chat_id):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    hack = await create_hack(app, chat_id, title="Хакатон <команды>")
    await create_hack(app, FOREIGN_CHAT, slug="foreign", title="Чужой хакатон")
    finished = await create_hack(app, chat_id, slug="finished", title="Завершённый хакатон")
    await app["storage"].hackathons.archive(finished.id, finished.end_at)
    await app["storage"].hackathons.add_member(hack.id, ADMIN_ID)
    await send(app, "/hack", chat_id=chat_id)
    text = await shown(app, ADMIN)
    assert "Хакатон <команды>" in unescape(text) and "admin_user" in text
    assert "Чужой хакатон" not in text and "Завершённый хакатон" not in text
    await send(app, "/hack h2026", chat_id=chat_id)
    text = await shown(app, ADMIN)
    assert "05.12.2026 10:00" in text and "Участники (1)" in text
    await send(app, "/hack nope", chat_id=chat_id)
    assert "не найден, список: /hack" in await shown(app, ADMIN)
    await send(app, "/hack h2026 extra", chat_id=chat_id)
    assert "Формат: /hack" in await shown(app, ADMIN)


async def test_github_shows_only_callers_account_without_token(app):
    app["ctx"].github.token = None
    await app["storage"].github_links.set_login(USER_ID, "alice-gh")
    await app["storage"].github_links.set_login(ADMIN_ID, "admin-gh")
    await send(app, "/github", ALICE)
    text = app["session"].last_text()
    assert 'href="https://github.com/alice-gh"' in text
    assert "admin-gh" not in text and "Интеграция: не настроена" in text


async def test_personal_profile_button_uses_presser(app):
    await send(app, "/me", ALICE)
    assert app["session"].last_text() == "👀 /me"
    assert f"id: {USER_ID}" in await shown(app, ALICE)
    assert f"id: {ADMIN_ID}" in await shown(app, ADMIN)


async def test_long_hack_list_goes_to_chat(app):
    for index in range(3):
        await create_hack(app, slug=f"hack{index}")
    await send(app, "/hack", ALICE)
    last = [m for m in app["session"].requests if isinstance(m, SendMessage)][-1]
    assert last.reply_markup is None
    assert all(f"hack{index}" in last.text for index in range(3))


async def test_expired_popup(app):
    requests = await press(app, "pop:999999")
    answers = [m for m in requests if isinstance(m, AnswerCallbackQuery)]
    assert answers[-1].text == "Ответ устарел, повторите команду"


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT, ADMIN_ID])
async def test_removed_commands_are_not_handled(app, chat_id):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    hack = await create_hack(app, chat_id)
    removed = [
        "start",
        "whoami",
        "chatid",
        "tz",
        "hacks",
        "left",
        "links",
        "history",
        "stats",
        "tasks",
        "task",
        "untask",
        "issues",
        "issue_new",
        "link_github",
        "unlink_github",
        "avail",
        "avail_set",
        "avail_reset",
        "hack_new",
        "hack_date",
        "hack_title",
        "hack_del",
        "repo",
        "add",
        "remove",
        "place",
        "site",
        "orgchat",
        "spec",
        "set_tz",
    ]
    for command in removed:
        before = len(app["session"].requests)
        await send(app, f"/{command} h2026", chat_id=chat_id)
        assert app["session"].requests[before:] == [], command
    assert await app["storage"].hackathons.get_by_id(hack.id) == hack
    assert await app["storage"].hackathons.members(hack.id) == []
    assert await app["storage"].tasks.active_for_hackathon(hack.id) == []


async def test_old_delete_button_and_spec_caption_do_not_mutate_data(app):
    hack = await create_hack(app)
    for actor in (ALICE, ADMIN):
        assert await press(app, f"hdel:{hack.id}:y", actor) == []
    document = Document(file_id="FILE_ID", file_unique_id="u", file_name="tz.pdf")
    message = Message(
        message_id=next(_ids),
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=ALLOWED_CHAT, type="supergroup"),
        from_user=ADMIN,
        caption="/spec h2026",
        document=document,
        caption_entities=[MessageEntity(type="bot_command", offset=0, length=5)],
    )
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), message=message))
    assert await app["storage"].hackathons.get_by_id(hack.id) == hack
    assert app["session"].texts() == []


async def test_foreign_chat_and_private(app):
    session = app["session"]
    await send(app, "/hack", ALICE, chat_id=FOREIGN_CHAT)
    assert any(isinstance(m, LeaveChat) and m.chat_id == FOREIGN_CHAT for m in session.requests)
    assert session.texts() == []
    await app["storage"].github_links.set_login(USER_ID, "alice-gh")
    await send(app, "/me", ALICE, chat_id=USER_ID)
    assert session.last_text() == f"Ваш user_id: <code>{USER_ID}</code>"
    before = len(session.requests)
    for command in ("/help", "/hack", "/github", "/whoami"):
        await send(app, command, ALICE, chat_id=USER_ID)
    assert await press(app, "my:me", ALICE, chat_id=USER_ID) == []
    assert session.requests[before:] == []


async def test_private_reminders_still_work(app):
    app["ctx"].settings.allowed_chat_ids.add(ADMIN_ID)
    hack = await create_hack(app, ADMIN_ID)
    await app["storage"].hackathons.add_member(hack.id, ADMIN_ID)
    await send(app, "/me", chat_id=ADMIN_ID)
    now = datetime(2026, 12, 5, 6, 0, tzinfo=UTC)
    await app["ctx"].reminders.tick(now)
    assert "до начала хакатона" in app["session"].last_text()
    assert "@admin_user" in app["session"].last_text()
    count = len(app["session"].texts())
    await app["ctx"].reminders.tick(now)
    assert len(app["session"].texts()) == count


async def test_setup_mode_uses_help_and_me(storage):
    settings = make_settings(allowed_chat_ids=set())
    session = MockSession()
    bot = Bot("123:TEST", session=session)
    github = GitHubClient(None)
    ctx = build_context(settings, bot, storage, github)
    app = {"bot": bot, "dp": build_dispatcher(ctx), "session": session}
    try:
        await send(app, "/help", ALICE, chat_id=FOREIGN_CHAT)
        assert f"<code>{FOREIGN_CHAT}</code>" in session.last_text()
        await send(app, "/me", ALICE, chat_id=USER_ID)
        assert session.last_text() == f"Ваш user_id: <code>{USER_ID}</code>"
        count = len(session.texts())
        await send(app, "/hack", ALICE, chat_id=FOREIGN_CHAT)
        await send(app, "/github", ALICE, chat_id=USER_ID)
        assert len(session.texts()) == count
        assert not any(isinstance(m, LeaveChat) for m in session.requests)
    finally:
        await github.close()
