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
    EditEphemeralMessageText,
    EditMessageText,
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
    ForceReply,
    InlineKeyboardMarkup,
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
        self.messages: list[Message] = []
        self.addresses: dict[tuple, Message] = {}

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None):
        self.requests.append(method)
        now = datetime(2026, 10, 26, tzinfo=UTC)
        if isinstance(method, (SendMessage, SendDocument)):
            ephemeral = getattr(method, "ephemeral_message_parameters", None)
            identifier = next(_ids)
            receiver = self.members.get(ephemeral.receiver_user_id) if ephemeral else None
            if ephemeral and receiver is None:
                receiver = User(id=ephemeral.receiver_user_id, is_bot=False, first_name="User")
            message = Message(
                message_id=0 if ephemeral else identifier,
                ephemeral_message_id=identifier if ephemeral else None,
                receiver_user=receiver,
                from_user=User(id=BOT_ID, is_bot=True, first_name="Bot"),
                date=now,
                chat=Chat(
                    id=method.chat_id, type="private" if method.chat_id > 0 else "supergroup"
                ),
                text=getattr(method, "text", None),
                reply_markup=method.reply_markup
                if isinstance(method.reply_markup, InlineKeyboardMarkup)
                else None,
            )
            self.messages.append(message)
            self.addresses[(method.chat_id, bool(ephemeral), identifier)] = message
            return message
        if isinstance(method, (EditEphemeralMessageText, EditMessageText)):
            ephemeral = isinstance(method, EditEphemeralMessageText)
            identifier = method.ephemeral_message_id if ephemeral else method.message_id
            address = (method.chat_id, ephemeral, identifier)
            message = self.addresses.get(address)
            if message:
                message = message.model_copy(
                    update={"text": method.text, "reply_markup": method.reply_markup}
                )
                self.addresses[address] = message
                self.messages.append(message)
            return True
        if isinstance(method, GetChatMember):
            if method.user_id in self.members:
                return ChatMemberMember(user=self.members[method.user_id])
            raise TelegramBadRequest(method=method, message="Bad Request: user not found")
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="hacks_bot")
        return True

    def texts(self) -> list[str]:
        return [
            m.text
            for m in self.requests
            if isinstance(m, (SendMessage, EditMessageText, EditEphemeralMessageText)) and m.text
        ]

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
    assert not any("Внутренняя ошибка" in (message.text or "") for message in session.messages)


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
    ephemeral = kw.pop("ephemeral", False)
    message = Message(
        message_id=0 if ephemeral else next(_ids),
        date=datetime.now(UTC),
        ephemeral_message_id=next(_ids) if ephemeral else None,
        chat=Chat(id=chat_id, type=chat_type, title=None if chat_id > 0 else "Team"),
        from_user=sender,
        text=text,
        entities=entities,
        **kw,
    )
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), message=message))


async def shown(app, presser: User) -> str:
    """Last text visible to the requesting user."""
    session = app["session"]
    for message in reversed(session.messages):
        if message.text and (
            (message.receiver_user and message.receiver_user.id == presser.id)
            or message.chat.id == presser.id
        ):
            return message.text
    return session.last_text()


async def create_hack(app, chat_id=ALLOWED_CHAT, slug="h2026", title="Хакатон"):
    return await app["storage"].hackathons.create(
        chat_id,
        slug,
        title,
        datetime(2026, 12, 1, 20, 59, tzinfo=UTC),
        datetime(2026, 12, 5, 7, 0, tzinfo=UTC),
        datetime(2026, 12, 7, 15, 0, tzinfo=UTC),
    )


async def press(app, data, presser=ADMIN, chat_id=ALLOWED_CHAT, source=None):
    message = (
        source
        or next(
            (
                message
                for message in reversed(app["session"].messages)
                if message.chat.id == chat_id
                and message.reply_markup
                and any(
                    item.callback_data == data
                    for row in message.reply_markup.inline_keyboard
                    for item in row
                )
            ),
            None,
        )
        or Message(
            message_id=next(_ids),
            date=datetime(2026, 10, 26, tzinfo=UTC),
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            text="👀",
        )
    )
    callback = CallbackQuery(
        id=str(next(_ids)), from_user=presser, chat_instance="x", data=data, message=message
    )
    before = len(app["session"].requests)
    await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), callback_query=callback))
    return app["session"].requests[before:]


def private_panel(app, actor=ALICE, chat_id=ALLOWED_CHAT):
    return next(
        message
        for message in reversed(app["session"].messages)
        if message.chat.id == chat_id
        and message.reply_markup
        and (chat_id > 0 or (message.receiver_user and message.receiver_user.id == actor.id))
        and any(
            item.callback_data and item.callback_data.startswith("hk:")
            for row in message.reply_markup.inline_keyboard
            for item in row
        )
    )


async def click(app, label, actor=ALICE, chat_id=ALLOWED_CHAT):
    panel = private_panel(app, actor, chat_id)
    item = next(
        item for row in panel.reply_markup.inline_keyboard for item in row if label in item.text
    )
    return await press(app, item.callback_data, actor, chat_id, source=panel)


async def answer_form(app, text, actor=ALICE, chat_id=ALLOWED_CHAT, **kwargs):
    prompt = next(
        message
        for message in reversed(app["session"].messages)
        if message.chat.id == chat_id
        and message.text
        and "Ответьте на это личное сообщение" in message.text
        and (chat_id > 0 or (message.receiver_user and message.receiver_user.id == actor.id))
    )
    await send(app, text, actor, chat_id, ephemeral=chat_id < 0, reply_to_message=prompt, **kwargs)


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
    assert "Активных хакатонов нет" in await shown(app, ADMIN)
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
    message = app["session"].messages[-1]
    labels = [item.text for row in message.reply_markup.inline_keyboard for item in row]
    assert any("Хакатон <команды>" in label for label in labels)
    assert not any("Чужой хакатон" in label or "Завершённый хакатон" in label for label in labels)
    await send(app, "/hack h2026", chat_id=chat_id)
    text = await shown(app, ADMIN)
    assert "Хакатон <команды>" in unescape(text) and "admin_user" in text
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


async def test_personal_profile_is_delivered_only_to_requesting_user(app):
    await send(app, "/me", ALICE)
    request = app["session"].requests[-1]
    assert request.ephemeral_message_parameters.receiver_user_id == USER_ID
    assert f"id: {USER_ID}" in await shown(app, ALICE)
    await send(app, "/me", ADMIN)
    assert f"id: {ADMIN_ID}" in await shown(app, ADMIN)


async def test_hack_menu_lists_cards_as_personal_buttons(app):
    for index in range(3):
        await create_hack(app, slug=f"hack{index}", title=f"hack{index}")
    await send(app, "/hack", ALICE)
    last = [m for m in app["session"].requests if isinstance(m, SendMessage)][-1]
    assert last.ephemeral_message_parameters.receiver_user_id == USER_ID
    labels = [item.text for row in last.reply_markup.inline_keyboard for item in row]
    assert all(any(f"hack{index}" in label for label in labels) for index in range(3))


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


async def test_non_admin_manual_creation_edit_and_ephemeral_input(app):
    await send(app, "/hack", ALICE, ephemeral=True)
    await click(app, "Создать вручную")
    for value in (
        "Хакатон без админов",
        "01.12.2026 23:59",
        "05.12.2026 10:00",
        "07.12.2026 18:00",
        "2",
        "3",
    ):
        await answer_form(app, value)
    for label, value in (
        ("Сайт", "https://hack.example/event"),
        ("Репозиторий GitHub", "team/project"),
        ("Чат организаторов", "https://t.me/organizers"),
    ):
        await click(app, label)
        await answer_form(app, value)
    await click(app, "ТЗ: ссылка или файл")
    await answer_form(
        app, "", document=Document(file_id="SPEC", file_unique_id="s", file_name="spec.pdf")
    )
    await click(app, "Чекпоинты")
    await click(app, "Добавить чекпоинт")
    await answer_form(app, "Промежуточная защита | 06.12.2026 12:00")
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    personal = [method for method in app["session"].requests if isinstance(method, SendMessage)]
    assert personal and all(
        method.ephemeral_message_parameters.receiver_user_id == USER_ID for method in personal
    )
    prompts = [method for method in personal if isinstance(method.reply_markup, ForceReply)]
    assert len(prompts) >= 10
    assert all(not method.reply_markup.selective for method in prompts)
    assert all(
        not method.reply_parameters.allow_sending_without_reply
        for method in personal
        if method.reply_parameters
    )
    await click(app, "Опубликовать")
    hack = (await app["storage"].hackathons.list(ALLOWED_CHAT))[0]
    assert (
        hack.created_by == USER_ID
        and hack.repo == REPO
        and hack.team_min == 2
        and hack.team_max == 3
    )
    assert hack.spec_file_id == "SPEC" and hack.org_chat_url == "https://t.me/organizers"
    checkpoints = await app["storage"].workflow.checkpoints(hack.id)
    assert len(checkpoints) == 1
    shared = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and not method.ephemeral_message_parameters
    ]
    assert (
        len(shared) == 1
        and shared[0].reply_markup.inline_keyboard[0][0].callback_data == f"hv:{hack.id}:yes"
    )
    bob = tg_user(3003, "bob_user", "Bob")
    await send(app, f"/hack {hack.slug}", bob)
    await click(app, "Редактировать", bob)
    await click(app, "Название", bob)
    await answer_form(app, "Изменено участником", bob)
    await click(app, "Сохранить изменения", bob)
    updated = await app["storage"].hackathons.get_by_id(hack.id)
    assert updated.title == "Изменено участником" and updated.revision == 1
    assert (await app["storage"].workflow.checkpoints(hack.id))[0].id == checkpoints[0].id
    assert any(
        isinstance(method, EditEphemeralMessageText) and method.receiver_user_id == bob.id
        for method in app["session"].requests
    )


async def test_model_menu_is_personal_owner_bound_paginated_and_persistent(app, monkeypatch):
    from unittest.mock import AsyncMock

    models = ["GigaChat", *[f"GigaChat-{i}" for i in range(10)]]
    monkeypatch.setattr(app["ctx"].hacks.llm, "available_models", AsyncMock(return_value=models))
    bob = tg_user(3003, "bob_user", "Bob")
    await send(app, "/hack", ALICE, ephemeral=True)
    await click(app, "Модель нейросети")
    await click(app, "GigaChat · Сбер")
    panel = private_panel(app)
    item = panel.reply_markup.inline_keyboard[0][0]
    await press(app, item.callback_data, bob, source=panel)
    assert await app["storage"].users.llm_preference(bob.id) is None
    await press(app, item.callback_data, ALICE, source=panel)
    assert await app["storage"].users.llm_preference(USER_ID) == ("gigachat", "GigaChat")
    await send(app, "/hack", ALICE, ephemeral=True)
    await click(app, "Модель нейросети")
    assert "GigaChat" in app["session"].last_text()
    await click(app, "GigaChat · Сбер")
    await click(app, "→")
    assert "Страница 2" in app["session"].last_text()
    await click(app, "GigaChat-9")
    assert await app["storage"].users.llm_preference(USER_ID) == ("gigachat", "GigaChat-9")
    await click(app, "Сбросить")
    assert await app["storage"].users.llm_preference(USER_ID) is None
    assert "openrouter/free" in app["session"].last_text()
    assert all(
        method.ephemeral_message_parameters is not None
        for method in app["session"].requests
        if isinstance(method, SendMessage)
    )


async def test_gigachat_menu_without_key_gives_private_setup_instructions(app):
    await send(app, "/hack", ALICE, ephemeral=True)
    await click(app, "Модель нейросети")
    await click(app, "GigaChat · Сбер")
    assert "GIGACHAT_CREDENTIALS" in app["session"].last_text()
    assert await app["storage"].users.llm_preference(USER_ID) is None


async def test_auto_url_requires_author_confirmation_and_deduplicates(app, monkeypatch):
    import json

    async def fetch(url):
        event = {
            "@type": "Event",
            "name": "Хакатон из ссылки",
            "startDate": "2026-12-05T10:00:00+03:00",
            "endDate": "2026-12-07T18:00:00+03:00",
            "offers": {"validThrough": "2026-12-01T23:59:00+03:00"},
            "minTeamSize": 2,
            "maxTeamSize": 4,
        }
        return '<script type="application/ld+json">' + json.dumps(event) + "</script>", url

    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch)
    await send(app, "https://hack.example/event", ALICE)
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    panel = private_panel(app)
    assert panel.receiver_user.id == USER_ID
    publish = next(
        item.callback_data
        for row in panel.reply_markup.inline_keyboard
        for item in row
        if "Опубликовать" in item.text
    )
    before = len(app["session"].requests)
    assert await press(app, publish, ADMIN, source=panel) == []
    assert len(app["session"].requests) == before
    await click(app, "Опубликовать")
    hacks = await app["storage"].hackathons.list(ALLOWED_CHAT)
    assert len(hacks) == 1 and hacks[0].title == "Хакатон из ссылки" and hacks[0].team_max == 4
    await send(app, "https://hack.example/event", ALICE)
    await click(app, "Опубликовать")
    assert len(await app["storage"].hackathons.list(ALLOWED_CHAT)) == 1
    shared = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and not method.ephemeral_message_parameters
    ]
    assert len(shared) == 1


async def test_votes_team_choices_and_duplicate_new_team_click(app):
    import asyncio

    from tests.test_hack_workflow import draft

    session, fields = await draft(app["storage"])
    hack_id = await app["storage"].workflow.publish_draft(session, fields)
    await app["ctx"].hack_notifications.flush()
    actors = [tg_user(uid, f"member_{uid}") for uid in (2002, 3003, 4004, 5005, 6006, 7007)]
    for actor in actors[:2]:
        await send(app, "/hack", actor)
        await press(app, f"hv:{hack_id}:yes", actor)
    teams = await app["storage"].workflow.teams(hack_id)
    assert teams[0].members == [2002, 3003]
    await send(app, "/hack", actors[2])
    await press(app, f"hv:{hack_id}:yes", actors[2])
    await click(app, "Вступить", actors[2])
    await send(app, "/hack", actors[3])
    await press(app, f"hv:{hack_id}:yes", actors[3])
    await click(app, "Сверх лимита", actors[3])
    await click(app, "Присоединиться сверх лимита", actors[3])
    assert (await app["storage"].workflow.teams(hack_id))[0].members == [2002, 3003, 4004, 5005]
    await send(app, "/hack", actors[4])
    await press(app, f"hv:{hack_id}:yes", actors[4])
    panel = private_panel(app, actors[4])
    new_team = next(
        item.callback_data
        for row in panel.reply_markup.inline_keyboard
        for item in row
        if "Новая команда" in item.text
    )
    await asyncio.gather(*(press(app, new_team, actors[4], source=panel) for _ in range(2)))
    await send(app, "/hack", actors[5])
    await press(app, f"hv:{hack_id}:yes", actors[5])
    teams = await app["storage"].workflow.teams(hack_id)
    assert len(teams) == 2 and teams[1].members == [6006, 7007]
    assert app["ctx"].settings.bot_admin_ids.isdisjoint(actor.id for actor in actors)
    assert any(
        isinstance(method, SendMessage)
        and method.ephemeral_message_parameters
        and method.ephemeral_message_parameters.replace_callback_query_message
        for method in app["session"].requests
    )


async def test_incomplete_or_stolen_draft_cannot_be_published(app):
    await send(app, "Хакатон без дат", ALICE)
    session = (await app["storage"].workflow.drafts(USER_ID, ALLOWED_CHAT))[0]
    panel = private_panel(app)
    forged = f"hk:{session.token}:{session.revision}:publish:"
    await press(app, forged, ALICE, source=panel)
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    forged_panel = panel.model_copy(update={"receiver_user": None})
    await press(app, forged, ADMIN, source=forged_panel)
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    assert not any(
        isinstance(method, SendMessage) and not method.ephemeral_message_parameters
        for method in app["session"].requests
    )


async def test_expired_ephemeral_ui_keeps_draft_recoverable(app):
    await send(app, "/hack", ALICE)
    await click(app, "Создать вручную")
    await answer_form(app, "Сохранённый черновик")
    drafts = await app["storage"].workflow.drafts(USER_ID, ALLOWED_CHAT)
    assert drafts[0].data["title"] == "Сохранённый черновик"
    app["session"].messages.clear()
    app["session"].addresses.clear()
    await send(app, "/hack", ALICE)
    await click(app, "Мои черновики")
    await click(app, "Сохранённый черновик")
    assert "Сохранённый черновик" in await shown(app, ALICE)


async def test_public_text_is_not_used_as_private_form_input(app):
    await send(app, "/hack", ALICE)
    await click(app, "Создать вручную")
    await send(app, "Это обычное сообщение в группе", ALICE)
    session = await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT)
    assert session.step == "input:title" and "title" not in session.data
    await answer_form(app, "Личное название")
    session = await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT)
    assert session.data["title"] == "Личное название"


async def test_personal_delivery_failure_never_falls_back_to_public_message(app, monkeypatch):
    original = app["session"].make_request

    async def fail_personal(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.ephemeral_message_parameters:
            app["session"].requests.append(method)
            raise TelegramBadRequest(method=method, message="Ephemeral delivery unavailable")
        return await original(bot, method, timeout)

    monkeypatch.setattr(app["session"], "make_request", fail_personal)
    await send(app, "/help", ALICE)
    sends = [method for method in app["session"].requests if isinstance(method, SendMessage)]
    assert sends and all(method.ephemeral_message_parameters for method in sends)


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT, USER_ID])
async def test_import_progress_stays_personal_and_fills_fields_before_completion(
    app,
    monkeypatch,
    chat_id,
):
    import asyncio
    from unittest.mock import AsyncMock

    clock = itertools.count(0, 2)
    monkeypatch.setattr("bot.handlers.import_progress.monotonic", lambda: next(clock))

    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    app["ctx"].hacks.llm.configured = True
    root, rules = "https://hack.example/event", "https://hack.example/rules"

    async def fetch(url):
        return (
            '<h1>Хакатон</h1><a href="/rules">Правила</a>'
            if url == root
            else "<p>Правила хакатона</p>",
            url,
        )

    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch)
    main_entered, main_release = asyncio.Event(), asyncio.Event()
    rules_entered, rules_release = asyncio.Event(), asyncio.Event()

    async def extract(post, html, url, tz, **kwargs):
        if url == root:
            main_entered.set()
            await main_release.wait()
            return {
                "title": "Хакатон из LLM",
                "is_hackathon": True,
                "team_max": 5,
                "registration_end": "2026-12-01T20:59:00+00:00",
                "start_at": "2026-12-05T07:00:00+00:00",
                "end_at": "2026-12-07T15:00:00+00:00",
                "follow_urls": [rules],
            }
        rules_entered.set()
        await rules_release.wait()
        return {"team_min": 2, "follow_urls": []}

    monkeypatch.setattr(app["ctx"].hacks.llm, "extract", AsyncMock(side_effect=extract))
    job = asyncio.create_task(send(app, root, ALICE, chat_id))
    try:
        await asyncio.wait_for(main_entered.wait(), timeout=1)
        panel = app["session"].messages[-1]
        assert "Изучаю хакатон" in panel.text and "Нейросеть извлекает" in panel.text
        assert not panel.reply_markup
        initial_id = panel.ephemeral_message_id if chat_id < 0 else panel.message_id
        main_release.set()
        await asyncio.wait_for(rules_entered.wait(), timeout=1)
        partial = app["session"].messages[-1]
        assert "Максимум в команде:</b> 5" in partial.text
        assert "Минимум в команде:</b> ищу" in partial.text
        assert await app["storage"].hackathons.list(chat_id) == []
        rules_release.set()
        await job
        complete = private_panel(app, ALICE, chat_id)
        assert "Проверьте данные" in complete.text
        assert "Минимум в команде:</b> 2" in complete.text
        assert (complete.ephemeral_message_id if chat_id < 0 else complete.message_id) == initial_id
        sends = [m for m in app["session"].requests if isinstance(m, SendMessage)]
        assert len(sends) == 1
        if chat_id < 0:
            assert sends[0].ephemeral_message_parameters.receiver_user_id == USER_ID
            edits = [m for m in app["session"].requests if isinstance(m, EditEphemeralMessageText)]
            assert edits and all(m.receiver_user_id == USER_ID for m in edits)
            assert not any(isinstance(m, EditMessageText) for m in app["session"].requests)
        assert await app["storage"].hackathons.list(chat_id) == []
    finally:
        main_release.set()
        rules_release.set()
        await job


async def test_expired_import_panel_is_replaced_only_with_personal_draft(app, monkeypatch):
    original = app["session"].make_request

    async def expired(bot, method, timeout=None):
        if isinstance(method, EditEphemeralMessageText):
            app["session"].requests.append(method)
            raise TelegramBadRequest(method=method, message="Ephemeral message expired")
        return await original(bot, method, timeout)

    monkeypatch.setattr(app["session"], "make_request", expired)
    await send(app, "Хакатон без дат", ALICE)
    panel = private_panel(app)
    assert "Проверьте данные" in panel.text
    assert await app["storage"].workflow.drafts(USER_ID, ALLOWED_CHAT)
    sends = [method for method in app["session"].requests if isinstance(method, SendMessage)]
    assert sends and all(method.ephemeral_message_parameters for method in sends)
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
