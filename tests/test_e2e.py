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
        now = datetime.now(UTC)
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
            else:
                raise TelegramBadRequest(method=method, message="message to edit not found")
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
            item.callback_data and item.callback_data.startswith(("hk:", "me:", "hv:", "hm:"))
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


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
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


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
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
    assert "Хакатон <команды>" in unescape(text) and "admin_user" not in text
    assert "05.12.2026 10:00" in text and "Участники: 1" in text
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
    assert (
        request.ephemeral_message_parameters.receiver_user_id
        if isinstance(request, SendMessage)
        else request.receiver_user_id
    ) == USER_ID
    assert f"id: {USER_ID}" in await shown(app, ALICE)
    await send(app, "/me", ADMIN)
    assert f"id: {ADMIN_ID}" in await shown(app, ADMIN)


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
async def test_settings_are_in_me_and_return_to_profile(app, chat_id):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    await app["storage"].users.set_llm_preference(USER_ID, "gigachat", "GigaChat-Pro")
    await send(app, "/hack", ALICE, chat_id=chat_id)
    panel = private_panel(app, chat_id=chat_id)
    assert all(
        "Модель" not in item.text for row in panel.reply_markup.inline_keyboard for item in row
    )
    await send(app, "/me", ALICE, chat_id=chat_id)
    assert "Профиль и настройки" in app["session"].last_text()
    assert "GigaChat-Pro" in app["session"].last_text()
    panel = private_panel(app, chat_id=chat_id)
    assert panel.reply_markup.inline_keyboard[0][0].callback_data.startswith("me:")
    await click(app, "Модель нейросети", chat_id=chat_id)
    await click(app, "К профилю", chat_id=chat_id)
    assert f"id: {USER_ID}" in app["session"].last_text()
    assert await app["storage"].users.llm_preference(USER_ID) == ("gigachat", "GigaChat-Pro")


async def test_old_hack_settings_buttons_redirect_without_changing_preference(app):
    await send(app, "/hack", ALICE)
    session = await app["storage"].workflow.new_session(
        USER_ID,
        ALLOWED_CHAT,
        "llm",
        {"_llm_provider": "gigachat", "_llm_models": ["GigaChat"]},
    )
    await press(app, f"hk:{session.token}:{session.revision}:llm_pick:0", ALICE)
    assert "/me" in app["session"].last_text()
    assert await app["storage"].users.llm_preference(USER_ID) is None


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


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
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
    assert session.texts() == []
    before = len(session.requests)
    for command in ("/help", "/hack", "/github", "/whoami"):
        await send(app, command, ALICE, chat_id=USER_ID)
    assert await press(app, "my:me", ALICE, chat_id=USER_ID) == []
    assert session.requests[before:] == []


async def test_legacy_private_hackathons_never_send_background_notifications(app):
    hack = await create_hack(app, ADMIN_ID)
    await app["storage"].hackathons.add_member(hack.id, ADMIN_ID)
    await send(app, "/me", chat_id=ADMIN_ID)
    await app["ctx"].reminders.tick(datetime(2026, 12, 5, 6, 0, tzinfo=UTC))
    await app["ctx"].hack_notifications.flush()
    await app["ctx"].tasks.poll_all()
    assert app["session"].requests == []


async def test_unconfigured_bot_does_not_reply_in_any_chat(storage):
    settings = make_settings(allowed_chat_ids=set())
    session = MockSession()
    bot = Bot("123:TEST", session=session)
    github = GitHubClient(None)
    ctx = build_context(settings, bot, storage, github)
    app = {"bot": bot, "dp": build_dispatcher(ctx), "session": session}
    try:
        await send(app, "/help", ALICE, chat_id=FOREIGN_CHAT)
        assert session.texts() == []
        await send(app, "/me", ALICE, chat_id=USER_ID)
        assert session.texts() == []
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
        "3",
    ):
        await answer_form(app, value)
    hack = (await app["storage"].hackathons.list(ALLOWED_CHAT))[0]
    await click(app, "Открыть хакатон")
    await click(app, "Редактировать")
    assert "Минимум" not in private_panel(app).text
    assert not any(
        "Минимум" in b.text for row in private_panel(app).reply_markup.inline_keyboard for b in row
    )
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
    assert len(await app["storage"].hackathons.list(ALLOWED_CHAT)) == 1
    personal = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and method.ephemeral_message_parameters
    ]
    assert len(personal) == 1
    assert personal[0].ephemeral_message_parameters.receiver_user_id == USER_ID
    hack = (await app["storage"].hackathons.list(ALLOWED_CHAT))[0]
    assert (
        hack.created_by == USER_ID
        and hack.repo == REPO
        and hack.team_min == 1
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
        and shared[0].reply_markup.inline_keyboard[0][0].callback_data == f"hm:{hack.id}"
    )
    bob = tg_user(3003, "bob_user", "Bob")
    await send(app, f"/hack {hack.slug}", bob)
    await click(app, "Редактировать", bob)
    await click(app, "Название", bob)
    await answer_form(app, "Изменено участником", bob)
    updated = await app["storage"].hackathons.get_by_id(hack.id)
    assert updated.title == "Изменено участником" and updated.revision == hack.revision + 1
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
    await send(app, "/me", ALICE, ephemeral=True)
    await click(app, "Модель нейросети")
    await click(app, "GigaChat · Сбер")
    panel = private_panel(app)
    item = panel.reply_markup.inline_keyboard[0][0]
    await press(app, item.callback_data, bob, source=panel)
    assert await app["storage"].users.llm_preference(bob.id) is None
    await press(app, item.callback_data, ALICE, source=panel)
    assert await app["storage"].users.llm_preference(USER_ID) == ("gigachat", "GigaChat")
    await send(app, "/me", ALICE, ephemeral=True)
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
    await send(app, "/me", ALICE, ephemeral=True)
    await click(app, "Модель нейросети")
    await click(app, "GigaChat · Сбер")
    assert "GIGACHAT_CREDENTIALS" in app["session"].last_text()
    assert await app["storage"].users.llm_preference(USER_ID) is None


async def test_auto_url_creates_without_draft_and_deduplicates(app, monkeypatch):
    import json

    async def fetch(url):
        if not url.endswith("/rules"):
            return '<h1>Хакатон</h1><a href="/rules">Положение</a>', url
        event = {
            "@type": "Event",
            "name": "Хакатон из ссылки",
            "startDate": "2026-12-05T10:00:00+03:00",
            "endDate": "2026-12-07T18:00:00+03:00",
            "offers": {"validThrough": "2026-12-01T23:59:00+03:00"},
            "minTeamSize": 2,
            "maxTeamSize": 4,
        }
        return (
            '<h1>Положение о проведении хакатона</h1><script type="application/ld+json">'
            + json.dumps(event)
            + "</script>",
            url,
        )

    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch)
    await send(app, "https://hack.example/event", ALICE)
    assert "создан" in private_panel(app).text
    assert await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT) is not None
    assert all(
        "черновик" not in item.text.lower()
        for row in private_panel(app).reply_markup.inline_keyboard
        for item in row
    )
    hacks = await app["storage"].hackathons.list(ALLOWED_CHAT)
    assert len(hacks) == 1 and hacks[0].title == "Хакатон из ссылки" and hacks[0].team_max == 4
    await send(app, "https://hack.example/event", ALICE)
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
    hack_id = await app["storage"].workflow.save_hack(session, fields)
    await app["ctx"].hack_notifications.flush()
    actors = [tg_user(uid, f"member_{uid}") for uid in (2002, 3003, 4004, 5005, 6006, 7007)]
    for actor in actors[:2]:
        await send(app, "/hack", actor)
        await press(app, f"hv:{hack_id}:yes", actor)
    teams = await app["storage"].workflow.teams(hack_id)
    assert teams[0].members == [2002, 3003]
    await send(app, "/hack", actors[2])
    await press(app, f"hv:{hack_id}:yes", actors[2])
    await click(app, "Команды", actors[2])
    panel = private_panel(app, actors[2])
    assert all(f"@member_{actor.id}" in panel.text for actor in actors[:3])
    assert not any(
        word in item.text
        for row in panel.reply_markup.inline_keyboard
        for item in row
        for word in ("Новая команда", "Состав:", "Вступить", "Сверх лимита")
    )
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
    personal = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and method.ephemeral_message_parameters
    ]
    assert len(personal) == 2 * len(actors)
    assert all(
        len([r for r in personal if r.ephemeral_message_parameters.receiver_user_id == actor.id])
        == 2
        for actor in actors
    )


async def test_incomplete_import_opens_input_and_cancel_discards_it_without_a_draft(app):
    await send(app, "Хакатон без дат", ALICE)
    assert "Заполните недостающие данные" in app["session"].last_text()
    assert "<b>Начало:</b> —" in app["session"].last_text()
    assert "черновик" not in app["session"].last_text().lower()
    assert (await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT)).step == (
        "form:registration_end"
    )
    await click(app, "Отмена")
    assert (await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT)).step == "menu"
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []


async def test_partial_link_import_keeps_found_fields_and_finishes_in_same_private_panel(
    app, monkeypatch
):
    async def fetch(url):
        if url.endswith("/rules"):
            return (
                "<h1>Положение о проведении хакатона</h1>"
                "<p>Начало 05.12.2026 10:00</p><p>Конец 07.12.2026 18:00</p>"
                "<p>Команда от 2 до 4 человек</p>",
                url,
            )
        return '<h1>Хакатон</h1><a href="/rules">Положение</a>', url

    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch)
    await send(app, "https://hack.example/event", ALICE)
    panel = private_panel(app)
    assert "<b>Конец регистрации:</b> —" in panel.text
    assert "<b>Максимум в команде:</b> 4" in panel.text
    assert "<b>Сайт:</b> https://hack.example/event" in panel.text
    assert "черновик" not in panel.text.lower()
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    assert not any(
        isinstance(method, SendMessage) and not method.ephemeral_message_parameters
        for method in app["session"].requests
    )
    await click(app, "Изменить данные")
    await click(app, "Название")
    await answer_form(app, "Исправленное название")
    await send(app, "01.12.2026 23:59", ALICE)  # Public messages do not fill a personal form.
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    await answer_form(app, "01.12.2026 23:59")
    hack = (await app["storage"].hackathons.list(ALLOWED_CHAT))[0]
    assert hack.title == "Исправленное название" and hack.team_max == 4
    assert hack.start_at == datetime(2026, 12, 5, 7, tzinfo=UTC)
    assert hack.repo is None and hack.org_chat_url is None
    assert private_panel(app).ephemeral_message_id == panel.ephemeral_message_id
    forms = await app["storage"].db.fetchall("SELECT * FROM hack_sessions WHERE step LIKE 'form:%'")
    assert forms == []
    personal = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and method.ephemeral_message_parameters
    ]
    public = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and not method.ephemeral_message_parameters
    ]
    assert len(personal) == len(public) == 1
    assert personal[0].ephemeral_message_parameters.receiver_user_id == USER_ID


async def test_legacy_publish_is_disabled(app):
    await send(app, "/hack", ALICE)
    session = await app["storage"].workflow.new_session(USER_ID, ALLOWED_CHAT, "preview")
    forged = f"hk:{session.token}:{session.revision}:publish:"
    await press(app, forged, ALICE)
    assert "Черновиков больше нет" in app["session"].last_text()
    await press(app, forged, ADMIN)
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    assert not any(
        isinstance(method, SendMessage) and not method.ephemeral_message_parameters
        for method in app["session"].requests
    )


async def test_missing_panel_is_replaced_privately_and_unfinished_creation_is_discarded(app):
    await send(app, "/hack", ALICE)
    await click(app, "Создать вручную")
    await answer_form(app, "Незавершённое создание")
    old_id = private_panel(app).ephemeral_message_id
    app["session"].addresses.clear()
    await send(app, "/hack", ALICE)
    panel = private_panel(app)
    assert panel.ephemeral_message_id != old_id
    assert all(
        "черновик" not in item.text.lower()
        for row in panel.reply_markup.inline_keyboard
        for item in row
    )
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    rows = await app["storage"].db.fetchall("SELECT * FROM hack_sessions WHERE step LIKE 'form:%'")
    assert rows == []


async def test_public_text_is_not_used_as_private_form_input(app):
    await send(app, "/hack", ALICE)
    await click(app, "Создать вручную")
    await send(app, "Это обычное сообщение в группе", ALICE)
    session = await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT)
    assert session.step == "form:title" and "title" not in session.data
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


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
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
            else "<h1>Положение о проведении хакатона</h1>",
            url,
        )

    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch)
    main_entered, main_release = asyncio.Event(), asyncio.Event()
    rules_entered, rules_release = asyncio.Event(), asyncio.Event()

    async def extract(post, html, url, tz, **kwargs):
        assert post == "" and url != root
        if url == rules:
            main_entered.set()
            await main_release.wait()
            return {
                "title": "Хакатон из LLM",
                "is_hackathon": True,
                "team_max": 5,
                "registration_end": "2026-12-01T20:59:00+00:00",
                "start_at": "2026-12-05T07:00:00+00:00",
                "end_at": "2026-12-07T15:00:00+00:00",
                "follow_urls": [],
            }
        assert kwargs["review_issues"]
        rules_entered.set()
        await rules_release.wait()
        return {"registration_end": "2026-12-01T20:59:00+00:00"}

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
        assert "Минимум" not in partial.text
        assert await app["storage"].hackathons.list(chat_id) == []
        rules_release.set()
        await job
        complete = private_panel(app, ALICE, chat_id)
        assert "создан" in complete.text if chat_id < 0 else "Участники:" in complete.text
        assert (await app["storage"].hackathons.list(chat_id))[0].team_min == 1
        assert (complete.ephemeral_message_id if chat_id < 0 else complete.message_id) == initial_id
        sends = [
            m
            for m in app["session"].requests
            if isinstance(m, SendMessage) and (chat_id > 0 or m.ephemeral_message_parameters)
        ]
        assert len(sends) == 1
        if chat_id < 0:
            assert sends[0].ephemeral_message_parameters.receiver_user_id == USER_ID
            edits = [m for m in app["session"].requests if isinstance(m, EditEphemeralMessageText)]
            assert edits and all(m.receiver_user_id == USER_ID for m in edits)
            assert not any(isinstance(m, EditMessageText) for m in app["session"].requests)
        assert len(await app["storage"].hackathons.list(chat_id)) == 1
    finally:
        main_release.set()
        rules_release.set()
        await job


async def test_expired_progress_panel_is_replaced_only_with_private_completion_form(
    app, monkeypatch
):
    original = app["session"].make_request

    async def expired(bot, method, timeout=None):
        if isinstance(method, EditEphemeralMessageText):
            app["session"].requests.append(method)
            raise TelegramBadRequest(method=method, message="Ephemeral message expired")
        return await original(bot, method, timeout)

    monkeypatch.setattr(app["session"], "make_request", expired)
    await send(app, "Хакатон без дат", ALICE)
    assert "Заполните недостающие данные" in app["session"].last_text()
    assert (await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT)).step == (
        "form:registration_end"
    )
    sends = [method for method in app["session"].requests if isinstance(method, SendMessage)]
    assert sends and all(method.ephemeral_message_parameters for method in sends)
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
async def test_commands_create_new_panels_while_manual_form_and_result_reuse_current_panel(
    app, chat_id
):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    await send(app, "/hack", ALICE, chat_id)
    initial = private_panel(app, ALICE, chat_id)
    initial_id = initial.ephemeral_message_id if chat_id < 0 else initial.message_id
    await click(app, "Создать вручную", chat_id=chat_id)
    for value in (
        "Один экран",
        "01.12.2026 23:59",
        "05.12.2026 10:00",
        "07.12.2026 18:00",
        "5",
    ):
        await answer_form(app, value, chat_id=chat_id)
    assert len(await app["storage"].hackathons.list(chat_id)) == 1
    assert private_panel(app, ALICE, chat_id).ephemeral_message_id == initial_id
    await send(app, "/me", ALICE, chat_id)
    profile_id = private_panel(app, ALICE, chat_id).ephemeral_message_id
    assert profile_id != initial_id
    await send(app, "/hack", ALICE, chat_id)
    current = private_panel(app, ALICE, chat_id)
    assert (current.ephemeral_message_id if chat_id < 0 else current.message_id) not in {
        initial_id,
        profile_id,
    }
    personal = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and (chat_id > 0 or method.ephemeral_message_parameters)
    ]
    assert len(personal) == 3
    assert all(
        "черновик" not in item.text.lower()
        for row in current.reply_markup.inline_keyboard
        for item in row
    )


async def test_personal_panels_are_isolated_by_user_and_concurrent_commands_each_send_a_reply(
    app, monkeypatch
):
    import asyncio

    original = app["session"].make_request

    async def delayed(bot, method, timeout=None):
        if isinstance(method, SendMessage):
            await asyncio.sleep(0.01)
        return await original(bot, method, timeout)

    monkeypatch.setattr(app["session"], "make_request", delayed)
    await asyncio.gather(send(app, "/help", ALICE), send(app, "/me", ALICE))
    sends = [method for method in app["session"].requests if isinstance(method, SendMessage)]
    assert len(sends) == 2
    alice_panel = private_panel(app)
    bob = tg_user(3003, "bob_user", "Bob")
    await send(app, "/me", bob)
    bob_panel = private_panel(app, bob)
    assert alice_panel.ephemeral_message_id != bob_panel.ephemeral_message_id
    await send(app, "/hack", ALICE)
    assert private_panel(app).ephemeral_message_id != alice_panel.ephemeral_message_id
    bob_address = (ALLOWED_CHAT, True, bob_panel.ephemeral_message_id)
    assert "id: 3003" in app["session"].addresses[bob_address].text


async def test_background_progress_does_not_overwrite_a_newer_menu(app, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    entered, release, tick, tick_done = (asyncio.Event() for _ in range(4))
    app["ctx"].hacks.llm.configured = True

    async def fetch(url):
        return "<h1>Положение о проведении хакатона</h1>", url

    async def extract(*args, **kwargs):
        if not kwargs.get("review_issues"):
            entered.set()
            await release.wait()
        return {
            "is_hackathon": True,
            "title": "Фоновый импорт",
            "team_max": 5,
            "registration_end": "2026-12-01T20:59:00+00:00",
            "start_at": "2026-12-05T07:00:00+00:00",
            "end_at": "2026-12-07T15:00:00+00:00",
        }

    async def heartbeat(progress):
        await tick.wait()
        await progress.render()
        tick_done.set()

    monkeypatch.setattr("bot.services.hackathons.fetch_public_page", fetch)
    monkeypatch.setattr(app["ctx"].hacks.llm, "extract", AsyncMock(side_effect=extract))
    monkeypatch.setattr("bot.handlers.import_progress.ImportProgress.tick", heartbeat)
    job = asyncio.create_task(send(app, "https://hack.example/rules", ALICE))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await send(app, "/me", ALICE)
        tick.set()
        await asyncio.wait_for(tick_done.wait(), timeout=1)
        assert "Профиль и настройки" in app["session"].last_text()
    finally:
        release.set()
        await job
    assert len(await app["storage"].hackathons.list(ALLOWED_CHAT)) == 1


@pytest.mark.parametrize("command", ["/help", "/me", "/github"])
async def test_leaving_form_for_another_command_discards_unfinished_input(app, command):
    await send(app, "/hack", ALICE)
    await click(app, "Создать вручную")
    await answer_form(app, "Незавершённое создание")
    await send(app, command, ALICE)
    assert (
        await app["storage"].db.fetchall("SELECT 1 FROM hack_sessions WHERE step LIKE 'form:%'")
        == []
    )
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    sends = [method for method in app["session"].requests if isinstance(method, SendMessage)]
    assert len(sends) == 2


async def test_invalid_manual_dates_are_retried_on_same_panel_before_creation(app):
    await send(app, "/hack", ALICE)
    await click(app, "Создать вручную")
    await answer_form(app, "Проверка дат")
    await answer_form(app, "06.12.2026 23:59")
    await answer_form(app, "05.12.2026 10:00")
    assert "Окончание регистрации" in app["session"].last_text()
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []
    await answer_form(app, "07.12.2026 10:00")
    await answer_form(app, "06.12.2026 18:00")
    assert "Начало хакатона должно быть раньше" in app["session"].last_text()
    for value in ("09.12.2026 18:00", "5"):
        await answer_form(app, value)
    assert len(await app["storage"].hackathons.list(ALLOWED_CHAT)) == 1
    personal = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and method.ephemeral_message_parameters
    ]
    assert len(personal) == 1


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
async def test_compact_cards_hide_checkpoint_details_and_open_them_privately_in_place(app, chat_id):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    await send(app, "/hack", ALICE, chat_id)
    initial = private_panel(app, chat_id=chat_id)
    panel_id = initial.ephemeral_message_id if chat_id < 0 else initial.message_id
    hack = await create_hack(app, chat_id=chat_id)
    editing = await app["ctx"].hacks.edit(hack.id, USER_ID)
    editing.data["checkpoints"] = [
        {"title": "Промежуточная защита", "at": "2026-12-06T09:00:00+00:00"},
        {"title": "Финальное демо", "at": "2026-12-07T09:00:00+00:00"},
    ]
    await app["ctx"].hacks.publish(editing)
    # Historical choices stay stored, but are not offered in the compact UI.
    await app["storage"].workflow.vote(hack.id, USER_ID, "maybe")
    await app["ctx"].hack_notifications.flush()
    shared = next(
        message
        for message in reversed(app["session"].messages)
        if not message.receiver_user and message.reply_markup
    )
    shared_buttons = [item for row in shared.reply_markup.inline_keyboard for item in row]
    assert all("Думаю" not in item.text and "Голоса" not in item.text for item in shared_buttons)
    assert "Промежуточная защита" not in shared.text and "Финальное демо" not in shared.text
    assert "🤔" not in shared.text
    checkpoint_button = next(item for item in shared_buttons if "Чекпоинты" in item.text)
    await press(app, checkpoint_button.callback_data, ALICE, chat_id, source=shared)
    details = private_panel(app, chat_id=chat_id)
    assert details.ephemeral_message_id != panel_id
    assert app["session"].addresses[(chat_id, True, panel_id)].text == initial.text
    panel_id = details.ephemeral_message_id
    if chat_id < 0:
        assert details.receiver_user.id == USER_ID
    assert "Промежуточная защита" in details.text and "Финальное демо" in details.text
    assert "06.12.2026 12:00" in details.text
    assert [item.text for row in details.reply_markup.inline_keyboard for item in row] == ["Назад"]
    await click(app, "Назад", chat_id=chat_id)
    compact = private_panel(app, chat_id=chat_id)
    assert (compact.ephemeral_message_id if chat_id < 0 else compact.message_id) == panel_id
    assert "Промежуточная защита" not in compact.text and "Финальное демо" not in compact.text
    assert all(
        "Думаю" not in item.text and "Голоса" not in item.text
        for row in compact.reply_markup.inline_keyboard
        for item in row
    )
    await click(app, "Чекпоинты", chat_id=chat_id)
    assert "Финальное демо" in private_panel(app, chat_id=chat_id).text
    assert len(await app["storage"].workflow.checkpoints(hack.id)) == 2
    assert (await app["storage"].workflow.votes(hack.id))["maybe"] == [USER_ID]
    personal = [
        method
        for method in app["session"].requests
        if isinstance(method, SendMessage) and (chat_id > 0 or method.ephemeral_message_parameters)
    ]
    assert len(personal) == 2
    assert personal[-1].ephemeral_message_parameters.replace_callback_query_message


async def test_empty_checkpoint_list_does_not_add_buttons_to_cards(app):
    hack = await create_hack(app)
    await send(app, f"/hack {hack.slug}", ALICE)
    card = private_panel(app)
    assert not any(
        "Чекпоинты" in item.text for row in card.reply_markup.inline_keyboard for item in row
    )
    text, markup = await app["ctx"].hack_notifications.card(hack)
    assert not any("Чекпоинты" in item.text for row in markup.inline_keyboard for item in row)
    assert "чекпоинт" not in text.lower()


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
@pytest.mark.parametrize("answer", [None, "Незавершённый хакатон"])
async def test_cancel_creation_works_on_current_prompt_without_leaving_the_panel(
    app, chat_id, answer
):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    await send(app, "/hack", ALICE, chat_id)
    initial = private_panel(app, chat_id=chat_id)
    panel_id = initial.ephemeral_message_id if chat_id < 0 else initial.message_id
    await click(app, "Создать вручную", chat_id=chat_id)
    if answer is not None:
        await answer_form(app, answer, chat_id=chat_id)
    await click(app, "Отмена", chat_id=chat_id)
    current = private_panel(app, chat_id=chat_id)
    assert (current.ephemeral_message_id if chat_id < 0 else current.message_id) == panel_id
    assert "Хакатоны" in current.text and "устарел" not in current.text
    session = await app["storage"].workflow.session(USER_ID, chat_id=chat_id)
    assert session.step == "menu"
    assert await app["storage"].hackathons.list(chat_id) == []
    assert (
        await app["storage"].db.fetchall("SELECT 1 FROM hack_sessions WHERE step LIKE 'form:%'")
        == []
    )


async def test_removed_import_button_still_allows_cancelling_a_legacy_prompt(app):
    await send(app, "/hack", ALICE)
    panel = private_panel(app)
    assert not any("Из поста" in b.text for row in panel.reply_markup.inline_keyboard for b in row)
    session = await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT)
    await press(app, f"hk:{session.token}:{session.revision}:import:", ALICE, source=panel)
    await click(app, "Отмена")
    assert "Хакатоны" in private_panel(app).text
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []


async def test_cancel_edit_prompt_returns_to_card_without_changing_hackathon(app):
    hack = await create_hack(app)
    await send(app, f"/hack {hack.slug}", ALICE)
    await click(app, "Редактировать")
    await click(app, "Название")
    await click(app, "Отмена")
    assert (await app["storage"].hackathons.get_by_id(hack.id)).title == hack.title
    assert "Регистрация до:" in private_panel(app).text
    assert "Введите:" not in private_panel(app).text


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
@pytest.mark.parametrize("choice", [None, "yes", "no", "maybe"])
async def test_participation_button_matches_membership_in_personal_and_shared_cards(
    app, chat_id, choice
):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    hack = await create_hack(app, chat_id=chat_id)
    if choice is not None:
        await app["storage"].workflow.vote(hack.id, USER_ID, choice)
    await send(app, f"/hack {hack.slug}", ALICE, chat_id)
    expected = "Отмена регистрации" if choice == "yes" else "✅ Участвую"
    buttons = private_panel(app, chat_id=chat_id).reply_markup.inline_keyboard
    assert [b.text for b in buttons[0]] == ["В меню"]
    if choice == "yes":
        assert buttons[-1][-1].text == expected and len(buttons[-1]) == 2
    assert [
        b.text for row in buttons for b in row if b.text in {"Отмена регистрации", "✅ Участвую"}
    ] == [expected]
    _, markup = await app["ctx"].hack_notifications.card(hack)
    assert [
        b.text for row in markup.inline_keyboard for b in row if b.callback_data.startswith("hv:")
    ] == ([expected] if chat_id > 0 else [])
    assert any(b.callback_data == f"hm:{hack.id}" for row in markup.inline_keyboard for b in row)
    if chat_id > 0:
        assert [b.text for b in markup.inline_keyboard[0]] == ["В меню"]
        if choice == "yes":
            assert markup.inline_keyboard[-1][-1].text == expected
            assert len(markup.inline_keyboard[-1]) == 2
        await press(app, markup.inline_keyboard[0][0].callback_data, ALICE, chat_id)
        assert "Хакатоны" in private_panel(app, chat_id=chat_id).text


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
async def test_participation_buttons_switch_in_place_after_joining_and_leaving(app, chat_id):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    hack = await create_hack(app, chat_id=chat_id)
    await send(app, f"/hack {hack.slug}", ALICE, chat_id)
    editing = await app["ctx"].hacks.edit(hack.id, USER_ID)
    await app["ctx"].hacks.publish(editing)
    await app["ctx"].hack_notifications.flush()
    await send(app, f"/hack {hack.slug}", ALICE, chat_id)
    initial = private_panel(app, chat_id=chat_id)
    panel_id = initial.ephemeral_message_id if chat_id < 0 else initial.message_id
    for label, participating in (
        ("Участвую", True),
        ("Отмена регистрации", False),
        ("Участвую", True),
    ):
        await click(app, label, chat_id=chat_id)
        current = private_panel(app, chat_id=chat_id)
        assert [b.text for b in current.reply_markup.inline_keyboard[0]] == ["В меню"]
        assert (current.ephemeral_message_id if chat_id < 0 else current.message_id) == panel_id
        assert (USER_ID in await app["storage"].hackathons.members(hack.id)) == participating
        assert [
            b.text
            for row in current.reply_markup.inline_keyboard
            for b in row
            if b.text in {"Отмена регистрации", "✅ Участвую"}
        ] == (["Отмена регистрации"] if participating else ["✅ Участвую"])


async def test_team_page_does_not_offer_join_again_after_registration_closes(app):
    hack = await create_hack(app)
    await send(app, f"/hack {hack.slug}", ALICE)
    await app["storage"].workflow.vote(hack.id, USER_ID, "yes")
    editing = await app["ctx"].hacks.edit(hack.id, USER_ID)
    editing.data["registration_end"] = "2026-01-01T00:00:00+00:00"
    await app["ctx"].hacks.publish(editing)
    await send(app, f"/hack {hack.slug}", ALICE)
    await click(app, "Команды")
    assert not any(
        "Участвую" in b.text for row in private_panel(app).reply_markup.inline_keyboard for b in row
    )


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
async def test_team_list_shows_all_rosters_with_telegram_aliases_in_place(app, chat_id):
    from tests.test_hack_workflow import draft

    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    _, fields = await draft(app["storage"])
    session = await app["storage"].workflow.new_session(USER_ID, chat_id, "preview")
    hack_id = await app["storage"].workflow.save_hack(session, fields)
    hack = await app["storage"].hackathons.get_by_id(hack_id)
    actors = [
        ALICE,
        tg_user(3003, None, "Без <алиаса>"),
        tg_user(4004, "bob"),
        tg_user(5005, "eve"),
    ]
    for actor in actors:
        app["session"].members[actor.id] = actor
        await app["ctx"].directory.remember(actor)
        await app["storage"].workflow.vote(hack_id, actor.id, "yes")
    await app["storage"].workflow.join_team(hack_id, actors[-1].id, None)
    await send(app, f"/hack {hack.slug}", ALICE, chat_id)
    initial = private_panel(app, chat_id=chat_id)
    await click(app, "Команды", chat_id=chat_id)
    panel = private_panel(app, chat_id=chat_id)
    assert panel.message_id == initial.message_id
    assert panel.ephemeral_message_id == initial.ephemeral_message_id
    for label in ("Команда 1", "Команда 2", "@alice_tg", "@bob", "@eve"):
        assert label in panel.text
    assert "Минимум" not in panel.text
    assert '<a href="tg://user?id=3003">Без &lt;алиаса&gt;</a>' in panel.text
    assert [b.text for row in panel.reply_markup.inline_keyboard for b in row] == [
        "В меню",
        "К карточке",
    ]


async def test_large_roster_pages_keep_every_alias_in_one_personal_panel(app):
    from bot.respond import html_to_plain
    from tests.test_hack_workflow import draft

    session, fields = await draft(app["storage"])
    fields["team_max"] = 100
    hack_id = await app["storage"].workflow.save_hack(session, fields)
    hack = await app["storage"].hackathons.get_by_id(hack_id)
    actors = [ALICE, *[tg_user(uid, f"member_{uid:025d}") for uid in range(3000, 3095)]]
    for actor in actors:
        app["session"].members[actor.id] = actor
        await app["ctx"].directory.remember(actor)
        await app["storage"].workflow.vote(hack_id, actor.id, "yes")
    await send(app, f"/hack {hack.slug}", ALICE)
    await click(app, "Команды")
    first = private_panel(app)
    panels = [first]
    while any(b.text == "→" for row in panels[-1].reply_markup.inline_keyboard for b in row):
        await click(app, "→")
        panels.append(private_panel(app))
        assert len(panels) < 10
    assert len(panels) > 1
    for panel in panels:
        assert panel.ephemeral_message_id == first.ephemeral_message_id
        assert len(html_to_plain(panel.text).encode("utf-16-le")) // 2 <= 3900
        assert "Команда 1" in panel.text
    text = "\n".join(panel.text for panel in panels)
    assert all(f"@{actor.username}" in text for actor in actors)
    assert len([m for m in app["session"].requests if isinstance(m, SendMessage)]) == 1


async def test_unassigned_participant_cannot_create_team_while_a_place_is_available(app):
    from tests.test_hack_workflow import draft

    session, fields = await draft(app["storage"])
    hack_id = await app["storage"].workflow.save_hack(session, fields)
    for uid in (3003, 4004, 5005, USER_ID):
        await app["storage"].workflow.vote(hack_id, uid, "yes")
    await app["storage"].workflow.vote(hack_id, 4004, "no")
    hack = await app["storage"].hackathons.get_by_id(hack_id)
    await send(app, f"/hack {hack.slug}", ALICE)
    await click(app, "Команды")
    labels = [b.text for row in private_panel(app).reply_markup.inline_keyboard for b in row]
    assert "Вступить: Команда 1" in labels
    assert not any("Новая команда" in label for label in labels)


async def open_task_assignment(app, hack, actor=ALICE, chat_id=ALLOWED_CHAT):
    await send(app, f"/hack {hack.slug}", actor, chat_id)
    await click(app, "Задачи", actor, chat_id)
    await click(app, "Назначить issue", actor, chat_id)
    await click(app, "#12", actor, chat_id)


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
async def test_issue_assignment_notifies_assignee_and_shows_personal_tasks_in_place(app, chat_id):
    import asyncio
    from unittest.mock import AsyncMock

    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    hack = await create_hack(app, chat_id)
    await app["storage"].hackathons.update_fields(hack.id, repo=REPO)
    await app["storage"].hackathons.add_member(hack.id, USER_ID)
    await app["storage"].github_links.set_login(USER_ID, "alice-gh")
    assign = AsyncMock(wraps=app["ctx"].github.add_assignees)
    app["ctx"].github.add_assignees = assign
    await open_task_assignment(app, hack, chat_id=chat_id)
    panel = private_panel(app, chat_id=chat_id)
    data = next(
        b.callback_data
        for row in panel.reply_markup.inline_keyboard
        for b in row
        if b.text == "@alice_tg"
    )
    await asyncio.gather(*(press(app, data, ALICE, chat_id, source=panel) for _ in range(2)))
    assign.assert_awaited_once_with(REPO, 12, ["alice-gh"])
    assigned = await app["storage"].tasks.active_for_user(USER_ID, [hack.id])
    assert len(assigned) == 1 and assigned[0].issue_number == 12 and assigned[0].seen_assigned
    notifications = [
        r
        for r in app["session"].requests
        if isinstance(r, SendMessage) and "Вам назначена" in r.text
    ]
    assert len(notifications) == 1 and notifications[0].chat_id == chat_id
    if chat_id < 0:
        assert notifications[0].ephemeral_message_parameters.receiver_user_id == USER_ID
    current = private_panel(app, chat_id=chat_id)
    assert current.message_id == panel.message_id
    assert current.ephemeral_message_id == panel.ephemeral_message_id
    await send(app, "/me", ALICE, chat_id)
    await click(app, "Мои задачи", chat_id=chat_id)
    current = private_panel(app, chat_id=chat_id)
    assert "Авторизация" in current.text and "не начато" in current.text
    assert f"https://github.com/{REPO}/issues/12" in current.text


async def test_non_admin_can_assign_to_another_participant_without_exposing_their_tasks(app):
    bob = tg_user(3003, "bob_tg", "Bob")
    await send(app, "/me", bob)
    hack = await create_hack(app)
    await app["storage"].hackathons.update_fields(hack.id, repo=REPO)
    await app["storage"].hackathons.add_member(hack.id, USER_ID)
    await app["storage"].hackathons.add_member(hack.id, bob.id)
    await open_task_assignment(app, hack)
    await click(app, "@bob_tg")
    assert "GitHub не привязан" in private_panel(app).text
    notification = next(
        r
        for r in app["session"].requests
        if isinstance(r, SendMessage) and "Вам назначена" in r.text
    )
    assert notification.ephemeral_message_parameters.receiver_user_id == bob.id
    await send(app, "/me", ALICE)
    await click(app, "Мои задачи")
    assert "задач нет" in private_panel(app).text
    await send(app, "/me", bob)
    await click(app, "Мои задачи", bob)
    assert "Авторизация" in private_panel(app, bob).text
    assert all(
        r.ephemeral_message_parameters is not None
        for r in app["session"].requests
        if isinstance(r, SendMessage)
    )


@pytest.mark.parametrize("change", ["repo", "left_chat", "not_registered", "closed"])
async def test_issue_assignment_rechecks_repository_recipient_and_issue(app, monkeypatch, change):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    hack = await create_hack(app)
    await app["storage"].hackathons.update_fields(hack.id, repo=REPO)
    await app["storage"].hackathons.add_member(hack.id, USER_ID)
    await open_task_assignment(app, hack)
    if change == "repo":
        await app["storage"].hackathons.update_fields(hack.id, repo="other/repository")
    elif change == "left_chat":
        app["session"].members.pop(USER_ID)
    elif change == "not_registered":
        await app["storage"].db.execute(
            "DELETE FROM hackathon_members WHERE hackathon_id=? AND user_id=?", (hack.id, USER_ID)
        )
    else:
        issue = await app["ctx"].github.get_issue(REPO, 12)
        monkeypatch.setattr(
            app["ctx"].github, "get_issue", AsyncMock(return_value=replace(issue, state="closed"))
        )
    await click(app, "@alice_tg")
    assert await app["storage"].tasks.active_for_hackathon(hack.id) == []
    assert not any(
        isinstance(r, SendMessage) and "Вам назначена" in r.text for r in app["session"].requests
    )


async def test_failed_personal_task_notification_keeps_task_without_public_fallback(
    app, monkeypatch
):
    hack = await create_hack(app)
    await app["storage"].hackathons.update_fields(hack.id, repo=REPO)
    await app["storage"].hackathons.add_member(hack.id, USER_ID)
    await open_task_assignment(app, hack)
    original = app["session"].make_request

    async def reject_notification(bot, method, timeout=None):
        if isinstance(method, SendMessage) and "Вам назначена" in method.text:
            app["session"].requests.append(method)
            raise TelegramBadRequest(method=method, message="Ephemeral delivery unavailable")
        return await original(bot, method, timeout)

    monkeypatch.setattr(app["session"], "make_request", reject_notification)
    await click(app, "@alice_tg")
    assert "уведомление не доставлено" in private_panel(app).text
    await send(app, "/me", ALICE)
    await click(app, "Мои задачи")
    assert "Авторизация" in private_panel(app).text
    assert all(
        r.ephemeral_message_parameters is not None
        for r in app["session"].requests
        if isinstance(r, SendMessage)
    )


async def test_saved_tasks_are_visible_when_github_is_disconnected_and_scoped_to_chat(app):
    from bot.db.models import TASK_NOT_STARTED, TASK_SOURCE_BOT

    hack = await create_hack(app)
    await app["storage"].hackathons.update_fields(hack.id, repo=REPO)
    other = await create_hack(app, FOREIGN_CHAT)
    for h, title in ((hack, "Моя задача"), (other, "Задача другого чата")):
        await app["storage"].tasks.upsert_active(
            h.id,
            USER_ID,
            12,
            title,
            f"https://github.com/{REPO}/issues/12",
            TASK_NOT_STARTED,
            TASK_SOURCE_BOT,
            False,
        )
    app["ctx"].github.token = None
    await send(app, "/me", ALICE)
    await click(app, "Мои задачи")
    assert (
        "Моя задача" in private_panel(app).text
        and "Задача другого чата" not in private_panel(app).text
    )
    await send(app, f"/hack {hack.slug}", ALICE)
    await click(app, "Задачи")
    assert (
        "GitHub не подключён" in private_panel(app).text and "Моя задача" in private_panel(app).text
    )
    assert not any(
        "Назначить" in b.text
        for row in private_panel(app).reply_markup.inline_keyboard
        for b in row
    )


@pytest.mark.parametrize("chat_id", [ALLOWED_CHAT])
async def test_github_account_can_be_linked_changed_and_unlinked_privately(app, chat_id):
    app["ctx"].settings.allowed_chat_ids.add(chat_id)
    await send(app, "/me", ALICE, chat_id)
    await click(app, "Аккаунт GitHub", chat_id=chat_id)
    await click(app, "Привязать аккаунт", chat_id=chat_id)
    initial = private_panel(app, chat_id=chat_id)
    await answer_form(app, "https://github.com/alice-gh", chat_id=chat_id)
    assert await app["storage"].github_links.get_login(USER_ID) == "alice-gh"
    current = private_panel(app, chat_id=chat_id)
    assert (
        current.message_id == initial.message_id
        and current.ephemeral_message_id == initial.ephemeral_message_id
    )
    await click(app, "Изменить аккаунт", chat_id=chat_id)
    await click(app, "Отмена", chat_id=chat_id)
    assert await app["storage"].github_links.get_login(USER_ID) == "alice-gh"
    await click(app, "Отвязать аккаунт", chat_id=chat_id)
    assert await app["storage"].github_links.get_login(USER_ID) is None


async def test_public_github_profile_is_linked_privately_without_integration_token(app):
    def handler(request):
        assert str(request.url) == "https://api.github.com/users/Aydar-art"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"login": "Aydar-art"})

    github = GitHubClient(None, transport=httpx.MockTransport(handler))
    app["ctx"].github = github
    try:
        await send(app, "/me", ALICE)
        await click(app, "Аккаунт GitHub")
        await click(app, "Привязать аккаунт")
        panel = private_panel(app)
        await answer_form(app, "https://github.com/Aydar-art")
        assert await app["storage"].github_links.get_login(USER_ID) == "Aydar-art"
        assert private_panel(app).ephemeral_message_id == panel.ephemeral_message_id
        assert not github.configured
        assert all(
            not isinstance(method, SendMessage) or method.ephemeral_message_parameters
            for method in app["session"].requests
        )
    finally:
        await github.close()


async def test_github_link_duplicate_and_invalid_input_preserve_previous_account(app):
    await app["storage"].github_links.set_login(USER_ID, "old-alice")
    await app["storage"].github_links.set_login(3003, "bob-gh")
    await send(app, "/me", ALICE)
    await click(app, "Аккаунт GitHub")
    await click(app, "Изменить аккаунт")
    await answer_form(app, "https://github.com/bob-gh")
    assert "уже привязан" in private_panel(app).text
    assert await app["storage"].github_links.get_login(USER_ID) == "old-alice"
    await answer_form(app, "https://evil.example/alice")
    assert await app["storage"].github_links.get_login(USER_ID) == "old-alice"
    await send(app, "/me", ALICE)
    assert await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT) is not None
    assert await app["storage"].hackathons.list(ALLOWED_CHAT) == []


async def test_issue_and_participant_lists_are_paginated_and_assign_correct_issue(app, monkeypatch):
    from unittest.mock import AsyncMock

    from bot.services.github import Issue

    hack = await create_hack(app)
    await app["storage"].hackathons.update_fields(hack.id, repo=REPO)
    issues = [
        Issue(n, f"Задача {n}", "open", f"https://github.com/{REPO}/issues/{n}", (), ())
        for n in range(12, 23)
    ]
    monkeypatch.setattr(app["ctx"].github, "list_open_issues", AsyncMock(return_value=issues))
    monkeypatch.setattr(
        app["ctx"].github,
        "get_issue",
        AsyncMock(side_effect=lambda repo, n: next(i for i in issues if i.number == n)),
    )
    actors = [ALICE, *[tg_user(uid, f"participant_{uid}") for uid in range(3000, 3010)]]
    for actor in actors:
        app["session"].members[actor.id] = actor
        await app["storage"].hackathons.add_member(hack.id, actor.id)
    await send(app, f"/hack {hack.slug}", ALICE)
    await click(app, "Задачи")
    await click(app, "Назначить issue")
    first = private_panel(app)
    await click(app, "→")
    await click(app, "#20")
    await click(app, "→")
    await click(app, "@participant_3009")
    assigned = await app["storage"].tasks.active_for_hackathon(hack.id)
    assert len(assigned) == 1 and (assigned[0].issue_number, assigned[0].user_id) == (20, 3009)
    assert private_panel(app).ephemeral_message_id == first.ephemeral_message_id


async def test_personal_task_list_pages_show_all_tasks_without_new_messages(app):
    from bot.db.models import TASK_NOT_STARTED, TASK_SOURCE_BOT

    hack = await create_hack(app)
    for number in range(1, 41):
        await app["storage"].tasks.upsert_active(
            hack.id,
            USER_ID,
            number,
            f"Issue {number}: " + "Подробности " * 8,
            f"https://github.com/{REPO}/issues/{number}",
            TASK_NOT_STARTED,
            TASK_SOURCE_BOT,
            False,
        )
    await send(app, "/me", ALICE)
    await click(app, "Мои задачи")
    first = private_panel(app)
    texts = [first.text]
    while any(
        b.text == "→" for row in private_panel(app).reply_markup.inline_keyboard for b in row
    ):
        await click(app, "→")
        current = private_panel(app)
        assert current.ephemeral_message_id == first.ephemeral_message_id
        texts.append(current.text)
        assert len(texts) < 10
    assert len(texts) > 1
    assert all(f"Issue {n}:" in "\n".join(texts) for n in range(1, 41))
    assert len([r for r in app["session"].requests if isinstance(r, SendMessage)]) == 1


async def test_issue_selection_rejects_unlisted_numbers_and_another_users_buttons(app):
    hack = await create_hack(app)
    await app["storage"].hackathons.update_fields(hack.id, repo=REPO)
    await app["storage"].hackathons.add_member(hack.id, USER_ID)
    await send(app, f"/hack {hack.slug}", ALICE)
    await click(app, "Задачи")
    await click(app, "Назначить issue")
    panel = private_panel(app)
    data = next(
        b.callback_data
        for row in panel.reply_markup.inline_keyboard
        for b in row
        if "#12" in b.text
    )
    bob = tg_user(3003, "bob_tg")
    before = len(app["session"].requests)
    await press(app, data, bob, source=panel)
    assert len(app["session"].requests) == before
    forged = data.rsplit(":", 1)[0] + ":99"
    await press(app, forged, ALICE, source=panel)
    assert "из текущего списка" in private_panel(app).text
    assert await app["storage"].tasks.active_for_hackathon(hack.id) == []


@pytest.mark.parametrize("kind", ["command", "text", "edited", "callback"])
async def test_private_updates_cannot_access_services_or_write_registry(app, kind):
    from unittest.mock import AsyncMock

    app["ctx"].github.get_issue = AsyncMock(side_effect=AssertionError("GitHub must not be called"))
    if kind == "command":
        for command in ("/help", "/hack", "/me", "/github", "/start"):
            await send(app, command, ALICE, USER_ID)
    elif kind == "text":
        await send(app, "https://github.com/team/project/issues/12", ALICE, USER_ID)
    elif kind == "edited":
        edited = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=USER_ID, type="private"),
            from_user=ALICE,
            text="/hack",
        )
        await app["dp"].feed_update(app["bot"], Update(update_id=next(_ids), edited_message=edited))
    else:
        for data in ("my:tasks", "hm:1", "hv:1:yes", "me:forged:0:github:"):
            await press(app, data, ALICE, USER_ID)
    assert app["session"].requests == []
    assert await app["storage"].users.get(USER_ID) is None
    assert await app["storage"].workflow.session(USER_ID) is None


async def test_legacy_cached_popup_does_not_disclose_ownerless_personal_data(app):
    popup_id = await app["storage"].popups.add(ALLOWED_CHAT, "Чужая личная задача")
    responses = await press(app, f"pop:{popup_id}", ALICE)
    assert not any("Чужая личная задача" in str(response) for response in responses)


@pytest.fixture
def panel_clock(monkeypatch):
    import sys

    import bot.respond

    current = [datetime.now(UTC).replace(microsecond=0)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz is not None else current[0].replace(tzinfo=None)

    monkeypatch.setattr(bot.respond, "datetime", Clock)
    monkeypatch.setattr(sys.modules[__name__], "datetime", Clock)
    return current


@pytest.mark.parametrize("elapsed", [0, 599, 600, 601])
async def test_each_command_replaces_personal_panel_regardless_of_age(app, panel_clock, elapsed):
    from datetime import timedelta

    await send(app, "/me", ALICE)
    original = private_panel(app)
    panel_clock[0] += timedelta(seconds=elapsed)
    await send(app, "/hack", ALICE)
    current = private_panel(app)
    assert current.ephemeral_message_id != original.ephemeral_message_id
    await send(app, "/me", ALICE)
    assert private_panel(app).ephemeral_message_id != current.ephemeral_message_id
    sent = [r for r in app["session"].requests if isinstance(r, SendMessage)]
    assert len(sent) == 3
    assert all(r.ephemeral_message_parameters.receiver_user_id == USER_ID for r in sent)


async def test_interactive_form_keeps_current_panel_even_after_ten_minutes(app, panel_clock):
    from datetime import timedelta

    await send(app, "/hack", ALICE)
    initial = private_panel(app)
    panel_clock[0] += timedelta(minutes=9)
    await click(app, "Создать вручную")
    assert private_panel(app).ephemeral_message_id == initial.ephemeral_message_id
    panel_clock[0] += timedelta(seconds=61)
    await answer_form(app, "Новая панель")
    current = private_panel(app)
    assert current.ephemeral_message_id == initial.ephemeral_message_id
    assert "Конец регистрации" in current.text
    session = await app["storage"].workflow.session(USER_ID, chat_id=ALLOWED_CHAT)
    assert session.data["_prompt_id"] == current.ephemeral_message_id
    await click(app, "Отмена")
    assert "Хакатоны" in private_panel(app).text
    assert private_panel(app).ephemeral_message_id == current.ephemeral_message_id


async def test_callback_reuses_available_panel_after_cache_loss_regardless_of_age(app, panel_clock):
    from datetime import timedelta

    from bot.respond import _PANELS

    await send(app, "/me", ALICE)
    initial = private_panel(app)
    _PANELS[app["bot"].session].clear()
    panel_clock[0] += timedelta(seconds=601)
    await click(app, "Мои задачи")
    current = private_panel(app)
    assert current.ephemeral_message_id == initial.ephemeral_message_id
    latest = next(r for r in reversed(app["session"].requests) if isinstance(r, SendMessage))
    assert latest.ephemeral_message_parameters.receiver_user_id == USER_ID
    assert not latest.ephemeral_message_parameters.replace_callback_query_message
    await click(app, "К профилю")
    assert private_panel(app).ephemeral_message_id == current.ephemeral_message_id


async def test_concurrent_commands_send_separate_replies_and_leave_other_users_panel_unchanged(
    app, panel_clock
):
    import asyncio
    from datetime import timedelta

    await send(app, "/me", ALICE)
    initial = private_panel(app)
    panel_clock[0] += timedelta(seconds=60)
    bob = tg_user(3003, "bob_tg")
    await send(app, "/me", bob)
    bobs_panel = private_panel(app, bob)
    panel_clock[0] += timedelta(seconds=541)
    await asyncio.gather(send(app, "/help", ALICE), send(app, "/me", ALICE))
    assert private_panel(app).ephemeral_message_id != initial.ephemeral_message_id
    await click(app, "Мои задачи", bob)
    assert private_panel(app, bob).ephemeral_message_id == bobs_panel.ephemeral_message_id
    sends = [r for r in app["session"].requests if isinstance(r, SendMessage)]
    assert len(sends) == 4
    assert all(r.ephemeral_message_parameters for r in sends)


async def test_each_participant_receives_only_their_command_panels_and_interactive_edits(app):
    actors = [ALICE, tg_user(3003, "bob_tg"), tg_user(4004, "carol_tg")]
    panels = {}
    for actor in actors:
        for command in ("/help", "/hack", "/github", "/me"):
            before = len(app["session"].requests)
            await send(app, command, actor, ephemeral=True)
            requests = app["session"].requests[before:]
            messages = [r for r in requests if isinstance(r, SendMessage)]
            assert len(messages) == 1
            assert messages[0].chat_id == ALLOWED_CHAT
            assert messages[0].ephemeral_message_parameters.receiver_user_id == actor.id
            assert not any(
                isinstance(r, (EditMessageText, EditEphemeralMessageText)) for r in requests
            )
        panels[actor.id] = private_panel(app, actor)
    assert len({p.ephemeral_message_id for p in panels.values()}) == len(actors)
    for actor in reversed(actors):
        before = len(app["session"].requests)
        await click(app, "Мои задачи", actor)
        await click(app, "К профилю", actor)
        requests = app["session"].requests[before:]
        assert not any(isinstance(r, SendMessage) for r in requests)
        edits = [r for r in requests if isinstance(r, EditEphemeralMessageText)]
        assert len(edits) == 2
        assert all(r.receiver_user_id == actor.id for r in edits)
        assert all(r.ephemeral_message_id == panels[actor.id].ephemeral_message_id for r in edits)
    for actor in actors:
        for other in actors:
            if other.id != actor.id:
                assert f"id: {other.id}" not in private_panel(app, actor).text


async def test_repeated_same_command_and_mention_without_entities_always_send_new_panel(app):
    await send(app, "/me", ALICE)
    first = private_panel(app)
    await send(app, "/me", ALICE)
    second = private_panel(app)
    await send(app, "/me@hacks_bot", ALICE, entities=[])
    third = private_panel(app)
    assert (
        len({first.ephemeral_message_id, second.ephemeral_message_id, third.ephemeral_message_id})
        == 3
    )
    sends = [r for r in app["session"].requests if isinstance(r, SendMessage)]
    assert len(sends) == 3
    assert all(r.ephemeral_message_parameters.receiver_user_id == USER_ID for r in sends)


async def test_open_new_shared_hack_overlays_clicked_card_without_editing_previous_personal_panel(
    app,
):
    from tests.test_hack_workflow import draft

    await send(app, "/me", ALICE)
    previous = private_panel(app)
    session, fields = await draft(app["storage"])
    hack_id = await app["storage"].workflow.save_hack(session, fields)
    await app["ctx"].hack_notifications.flush()
    shared = next(
        m for m in reversed(app["session"].messages) if not m.receiver_user and m.reply_markup
    )
    before = len(app["session"].requests)
    await press(app, f"hm:{hack_id}", ALICE, source=shared)
    requests = app["session"].requests[before:]
    messages = [r for r in requests if isinstance(r, SendMessage)]
    assert len(messages) == 1
    assert messages[0].ephemeral_message_parameters.receiver_user_id == USER_ID
    assert messages[0].ephemeral_message_parameters.replace_callback_query_message
    assert not any(isinstance(r, (EditMessageText, EditEphemeralMessageText)) for r in requests)
    assert (
        app["session"].addresses[(ALLOWED_CHAT, True, previous.ephemeral_message_id)].text
        == previous.text
    )
    assert app["session"].addresses[(ALLOWED_CHAT, False, shared.message_id)].text == shared.text
    opened = private_panel(app)
    await click(app, "Команды")
    assert private_panel(app).ephemeral_message_id == opened.ephemeral_message_id
    assert len([r for r in app["session"].requests[before:] if isinstance(r, SendMessage)]) == 1


async def test_callback_edits_its_own_personal_message_instead_of_latest_command_panel(app):
    hack = await create_hack(app)
    await send(app, "/hack", ALICE)
    clicked = private_panel(app)
    data = next(
        b.callback_data
        for row in clicked.reply_markup.inline_keyboard
        for b in row
        if b.text == hack.display_name
    )
    await send(app, "/me", ALICE)
    other = private_panel(app)
    requests = await press(app, data, ALICE, source=clicked)
    edits = [r for r in requests if isinstance(r, EditEphemeralMessageText)]
    assert len(edits) == 1 and edits[0].ephemeral_message_id == clicked.ephemeral_message_id
    assert edits[0].receiver_user_id == USER_ID
    assert not any(isinstance(r, (SendMessage, EditMessageText)) for r in requests)
    assert (
        app["session"].addresses[(ALLOWED_CHAT, True, other.ephemeral_message_id)].text
        == other.text
    )
    await click(app, "Команды")
    assert private_panel(app).ephemeral_message_id == clicked.ephemeral_message_id


async def test_missing_clicked_personal_panel_is_replaced_without_changing_another_panel(app):
    hack = await create_hack(app)
    await send(app, "/hack", ALICE)
    clicked = private_panel(app)
    data = next(
        b.callback_data
        for row in clicked.reply_markup.inline_keyboard
        for b in row
        if b.text == hack.display_name
    )
    await send(app, "/me", ALICE)
    other = private_panel(app)
    del app["session"].addresses[(ALLOWED_CHAT, True, clicked.ephemeral_message_id)]
    requests = await press(app, data, ALICE, source=clicked)
    messages = [r for r in requests if isinstance(r, SendMessage)]
    assert len(messages) == 1
    assert messages[0].ephemeral_message_parameters.receiver_user_id == USER_ID
    assert not messages[0].ephemeral_message_parameters.replace_callback_query_message
    assert (
        app["session"].addresses[(ALLOWED_CHAT, True, other.ephemeral_message_id)].text
        == other.text
    )
    assert private_panel(app).ephemeral_message_id not in {
        clicked.ephemeral_message_id,
        other.ephemeral_message_id,
    }
