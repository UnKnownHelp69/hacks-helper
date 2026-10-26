"""End-to-end: real Dispatcher + handlers, mocked Telegram session and GitHub API."""

from __future__ import annotations

import importlib
import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
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
                chat=Chat(id=method.chat_id, type="supergroup"),
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
        chat=Chat(id=last.chat_id, type="supergroup"),
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


async def test_full_flow(app):
    session = app["session"]

    # Alice introduces herself so the bot learns her user_id
    await send(app, "/me", ALICE)
    assert session.last_text() == "👀 /me"  # the answer itself is hidden behind a button
    assert f"id: {USER_ID}" in await shown(app, ALICE)

    # non-admin cannot create hackathons
    await send(app, "/hack_new h2026 01.12.2026 23:59 05.12.2026 10:00 07.12.2026 18:00", ALICE)
    assert await shown(app, ALICE) == "Эта команда доступна только администраторам бота"

    await send(app, "/hack_new h2026 01.12.2026 23:59 05.12.2026 10:00 07.12.2026 18:00 Хакатон")
    assert "✅ Создан" in await shown(app, ADMIN)
    hack = await app["storage"].hackathons.get(ALLOWED_CHAT, "h2026")
    assert hack is not None and hack.start_at == datetime(2026, 12, 5, 7, 0, tzinfo=UTC)

    await send(app, "/hack_new bad 05.12.2026 10:00 01.12.2026 23:59 07.12.2026 18:00")
    assert "не позже начала" in await shown(app, ADMIN)

    await send(app, "/add h2026 @alice_tg @ghost_user")
    text = await shown(app, ADMIN)
    assert "+alice_tg" in text and "Я ещё не знаю @ghost_user" in text
    assert await app["storage"].hackathons.members(hack.id) == [USER_ID]

    await send(app, "/repo h2026 https://github.com/team/project")
    assert "team/project" in await shown(app, ADMIN)

    await send(app, "/link_github alice-gh", ALICE)
    assert "GitHub alice-gh" in await shown(app, ALICE)

    await send(app, "/task h2026 12", ALICE)  # self-assignment
    assert "#12 Авторизация" in await shown(app, ALICE)
    tasks = await app["storage"].tasks.active_for_hackathon(hack.id)
    assert [(t.user_id, t.issue_number) for t in tasks] == [(USER_ID, 12)]

    await send(app, "/task h2026 @admin_user 12", ALICE)  # assigning others: admin only
    assert "только админ" in await shown(app, ALICE)

    await send(app, "/tasks h2026", ALICE)
    text = await shown(app, ALICE)
    assert "alice_tg" in text and "@alice_tg" not in text

    # "/tasks" alone gives a "my tasks" button computed for whoever presses it
    await send(app, "/tasks", ALICE)
    assert "#12 Авторизация" in await shown(app, ALICE)
    assert "admin_user: задач нет" in await shown(app, ADMIN)

    await send(app, "/issues h2026", ALICE)  # links + paging: always a chat message
    assert "#12 Авторизация" in session.last_text()

    await send(app, "/issue_new h2026 me Новая\nОписание", ALICE)
    assert "Новая" in await shown(app, ALICE)

    await send(app, "/avail_set ср 09:00-21:00", ALICE)
    assert "Ср: 09:00–21:00" in await shown(app, ALICE)

    await send(app, "/avail_set ср нет @admin_user", ALICE)
    assert "только админ" in await shown(app, ALICE)

    await send(app, "/set_tz Asia/Yekaterinburg", ALICE)
    assert await shown(app, ALICE) == "Эта команда доступна только администраторам бота"
    await send(app, "/set_tz Asia/Yekaterinburg")
    await send(app, "/tz", ALICE)
    text = await shown(app, ALICE)
    assert "Asia/Yekaterinburg" in text and "UTC+05:00" in text

    await send(app, "/left h2026", ALICE)
    assert "до окончания регистрации" in await shown(app, ALICE)

    await send(app, "/hacks", ALICE)
    assert "Хакатон (h2026)" in await shown(app, ALICE)

    await send(app, "/hack h2026", ALICE)
    assert "05.12.2026 12:00" in await shown(app, ALICE)  # shown in the new time zone

    await send(app, "/help@hacks_bot", ALICE)
    assert "🔒" in session.last_text()

    await send(app, "/place h2026 2")
    assert "после завершения" in await shown(app, ADMIN)

    await send(app, "/hack nope", ALICE)
    assert "не найден" in await shown(app, ALICE)

    # names are plain text: <code> renders as empty boxes in some Linux clients
    assert not any("<code>alice_tg" in t for t in session.texts())


async def test_long_answers_go_to_chat(app):
    session = app["session"]
    storage = app["storage"]
    await send(app, "/me", ALICE)
    await send(app, "/hack_new h1 01.12.2026 23:59 05.12.2026 10:00 07.12.2026 18:00")
    hack = await storage.hackathons.get(ALLOWED_CHAT, "h1")
    await storage.hackathons.add_member(hack.id, USER_ID)
    for number in range(1, 8):
        await storage.tasks.upsert_active(
            hack.id,
            USER_ID,
            number,
            f"Длинное название задачи {number}",
            "",
            "not_started",
            "bot",
            False,
        )
    await send(app, "/tasks", ALICE)
    last = [m for m in session.requests if isinstance(m, SendMessage)][-1]
    assert last.reply_markup is None and "Длинное название задачи 7" in last.text

    # an old "my tasks" button whose answer has grown too long posts it to the chat
    popup_owner = await shown_after_button(app, "my:tasks", ALICE)
    assert "Длинное название задачи 1" in popup_owner


async def shown_after_button(app, data: str, presser: User) -> str:
    message = Message(
        message_id=next(_ids),
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=ALLOWED_CHAT, type="supergroup"),
        text="👀",
    )
    callback = CallbackQuery(
        id=str(next(_ids)), from_user=presser, chat_instance="x", data=data, message=message
    )
    before = len(app["session"].requests)
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), callback_query=callback))
    new = app["session"].requests[before:]
    assert any(isinstance(m, AnswerCallbackQuery) and not m.show_alert for m in new)
    return "\n".join(m.text for m in new if isinstance(m, SendMessage))


async def test_expired_popup(app):
    text = await shown_after_button_alert(app, "pop:999999")
    assert text == "Ответ устарел, повторите команду"


async def shown_after_button_alert(app, data: str) -> str:
    message = Message(
        message_id=next(_ids),
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=ALLOWED_CHAT, type="supergroup"),
        text="👀",
    )
    callback = CallbackQuery(
        id=str(next(_ids)), from_user=ALICE, chat_instance="x", data=data, message=message
    )
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), callback_query=callback))
    answers = [m for m in app["session"].requests if isinstance(m, AnswerCallbackQuery)]
    return answers[-1].text


async def test_reply_and_spec_file(app):
    session = app["session"]
    await send(app, "/hack_new h1 01.12.2026 23:59 05.12.2026 10:00 07.12.2026 18:00")
    alice_msg = Message(
        message_id=500,
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=ALLOWED_CHAT, type="supergroup"),
        from_user=ALICE,
        text="привет",
    )
    await send(app, "/add h1", reply_to_message=alice_msg)
    hack = await app["storage"].hackathons.get(ALLOWED_CHAT, "h1")
    assert await app["storage"].hackathons.members(hack.id) == [USER_ID]

    doc = Document(file_id="FILE_ID", file_unique_id="u", file_name="tz.pdf")
    # a document with the command in its caption
    message = Message(
        message_id=next(_ids),
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=ALLOWED_CHAT, type="supergroup"),
        from_user=ADMIN,
        caption="/spec h1",
        document=doc,
        caption_entities=[MessageEntity(type="bot_command", offset=0, length=5)],
    )
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), message=message))
    assert "tz.pdf" in await shown(app, ADMIN)
    await send(app, "/site h1 https://hack.example.org")
    await send(app, "/links h1", ALICE)
    document = [m for m in session.requests if isinstance(m, SendDocument)][-1]
    assert document.document == "FILE_ID"
    assert "hack.example.org" in document.caption


async def test_foreign_chat_and_private(app):
    session = app["session"]
    await send(app, "/hacks", ALICE, chat_id=FOREIGN_CHAT)
    assert any(isinstance(m, LeaveChat) and m.chat_id == FOREIGN_CHAT for m in session.requests)
    assert session.texts() == []

    await send(app, "/whoami", ALICE, chat_id=USER_ID)
    assert session.last_text() == f"Ваш user_id: <code>{USER_ID}</code>"
    count = len(session.texts())
    await send(app, "/hacks", ALICE, chat_id=USER_ID)
    await send(app, "/me", ALICE, chat_id=USER_ID)
    assert len(session.texts()) == count


async def test_delete_confirmation_requires_admin(app):
    await send(app, "/hack_new h1 01.12.2026 23:59 05.12.2026 10:00 07.12.2026 18:00")
    hack = await app["storage"].hackathons.get(ALLOWED_CHAT, "h1")
    message = Message(
        message_id=1,
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=Chat(id=ALLOWED_CHAT, type="supergroup"),
        from_user=ADMIN,
        text="confirm?",
    )
    for presser in (ALICE, ADMIN):
        callback = CallbackQuery(
            id=str(next(_ids)),
            from_user=presser,
            chat_instance="x",
            data=f"hdel:{hack.id}:y",
            message=message,
        )
        await app["dp"].feed_update(
            app["bot"], Update(update_id=next(_ids), callback_query=callback)
        )
        exists = await app["storage"].hackathons.get_by_id(hack.id)
        assert (exists is not None) == (presser is ALICE)


async def test_setup_mode_chatid(storage):
    settings = make_settings(allowed_chat_ids=set())
    session = MockSession()
    bot = Bot("123:TEST", session=session)
    ctx = build_context(settings, bot, storage, GitHubClient(None))
    app = {"bot": bot, "dp": build_dispatcher(ctx), "session": session}
    await send(app, "/chatid", ALICE, chat_id=FOREIGN_CHAT)
    assert f"<code>{FOREIGN_CHAT}</code>" in session.last_text()
    await send(app, "/help", ALICE, chat_id=FOREIGN_CHAT)
    assert len(session.texts()) == 1
    assert not any(isinstance(m, LeaveChat) for m in session.requests)
