"""Security: allowed chats, leaving foreign chats, setup mode, admin check, sender_chat."""

from datetime import UTC, datetime

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
)

from bot.middlewares.admin import AdminOnlyMiddleware
from bot.middlewares.security import (
    ADMIN_ONLY_TEXT,
    SecurityMiddleware,
    command_name,
    is_bot_admin,
)
from tests.conftest import (
    ADMIN_ID,
    ALLOWED_CHAT,
    FOREIGN_CHAT,
    USER_ID,
    group,
    make_settings,
    message,
    update_with,
    user,
)

ANON_ADMIN_BOT_ID = 1087968824  # Telegram's GroupAnonymousBot
BOT_ID = 4242


@pytest.fixture
def replies(monkeypatch):
    sent: list[str] = []

    async def fake_reply(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "reply", fake_reply)
    return sent


def bot_added(chat_id: int, by: int = USER_ID, new_status: str = "member") -> Update:
    bot_user = user(BOT_ID, "hacks_bot", is_bot=True)
    new = (
        ChatMemberMember(user=bot_user) if new_status == "member" else ChatMemberLeft(user=bot_user)
    )
    return Update(
        update_id=1,
        my_chat_member=ChatMemberUpdated(
            chat=group(chat_id),
            from_user=user(by),
            date=datetime(2026, 10, 26, tzinfo=UTC),
            old_chat_member=ChatMemberLeft(user=bot_user),
            new_chat_member=new,
        ),
    )


# --- level 1: allowed chats ---


async def test_allowed_chat_passes(fake_bot):
    mw = SecurityMiddleware(make_settings())
    assert await mw.check(update_with(message("/help")), fake_bot)
    assert fake_bot.left == []


async def test_foreign_chat_message_is_dropped_and_bot_leaves_once(fake_bot, replies):
    mw = SecurityMiddleware(make_settings())
    msg = message("/hacks", chat=group(FOREIGN_CHAT))
    assert not await mw.check(update_with(msg), fake_bot)
    assert not await mw.check(update_with(msg), fake_bot)
    assert fake_bot.left == [FOREIGN_CHAT]
    assert replies == []  # nothing is written to the foreign chat
    assert fake_bot.sent == []


async def test_bot_added_to_foreign_chat_leaves_immediately(fake_bot):
    mw = SecurityMiddleware(make_settings())
    assert not await mw.check(bot_added(FOREIGN_CHAT), fake_bot)
    assert fake_bot.left == [FOREIGN_CHAT]
    assert fake_bot.sent == []


async def test_bot_removed_from_foreign_chat_does_not_call_leave(fake_bot):
    mw = SecurityMiddleware(make_settings())
    assert not await mw.check(bot_added(FOREIGN_CHAT, new_status="left"), fake_bot)
    assert fake_bot.left == []


async def test_bot_added_to_allowed_chat_is_processed(fake_bot):
    mw = SecurityMiddleware(make_settings())
    assert await mw.check(bot_added(ALLOWED_CHAT), fake_bot)
    assert fake_bot.left == []


async def test_private_chat_never_accepts_commands(fake_bot):
    mw = SecurityMiddleware(make_settings())
    private = Chat(id=USER_ID, type="private")
    assert not await mw.check(update_with(message("/me", chat=private)), fake_bot)
    for text in ("/help", "/github", "/hack", "/start", "hello", "/whoami"):
        assert not await mw.check(update_with(message(text, chat=private)), fake_bot), text
    assert fake_bot.left == []


async def test_private_chat_callbacks_stay_disabled_even_if_runtime_settings_are_corrupted(
    fake_bot,
):
    settings = make_settings()
    settings.allowed_chat_ids = {ADMIN_ID}
    mw = SecurityMiddleware(settings)
    msg = message("/hack", chat=Chat(id=ADMIN_ID, type="private"), from_user=user(ADMIN_ID))
    callback = CallbackQuery(
        id="private", from_user=user(ADMIN_ID), chat_instance="x", data="my:me", message=msg
    )
    assert not settings.is_chat_allowed(ADMIN_ID)
    assert not await mw.check(update_with(msg), fake_bot)
    assert not await mw.check(Update(update_id=1, callback_query=callback), fake_bot)
    assert not is_bot_admin(settings, callback)
    assert fake_bot.sent == [] and fake_bot.left == []


async def test_callback_from_foreign_chat_is_dropped(fake_bot):
    mw = SecurityMiddleware(make_settings())
    callback = CallbackQuery(
        id="1",
        from_user=user(ADMIN_ID),
        chat_instance="x",
        data="hdel:1:y",
        message=message("x", chat=group(FOREIGN_CHAT)),
    )
    assert not await mw.check(Update(update_id=1, callback_query=callback), fake_bot)


# --- setup mode (ALLOWED_CHAT_IDS empty) ---


async def test_without_configured_group_all_commands_are_ignored(fake_bot):
    mw = SecurityMiddleware(make_settings(allowed_chat_ids=set()))
    chat = group(FOREIGN_CHAT)
    assert not await mw.check(update_with(message("/help", chat=chat)), fake_bot)
    assert not await mw.check(update_with(message("/help@hacks_bot", chat=chat)), fake_bot)
    for text in ("/hack", "/github", "/me", "/chatid", "hello"):
        assert not await mw.check(update_with(message(text, chat=chat)), fake_bot), text
    private = Chat(id=USER_ID, type="private")
    assert not await mw.check(update_with(message("/me", chat=private)), fake_bot)
    assert not await mw.check(bot_added(FOREIGN_CHAT), fake_bot)
    assert fake_bot.left == []


async def test_after_setup_help_works_only_in_allowed_chats(fake_bot):
    mw = SecurityMiddleware(make_settings())
    assert await mw.check(update_with(message("/help")), fake_bot)
    assert not await mw.check(update_with(message("/help", chat=group(FOREIGN_CHAT))), fake_bot)
    assert fake_bot.left == [FOREIGN_CHAT]


# --- sender_chat / anonymous admins ---


async def test_sender_chat_commands_are_refused(fake_bot, replies):
    mw = SecurityMiddleware(make_settings())
    msg = message(
        "/hack_new x",
        from_user=user(ANON_ADMIN_BOT_ID, "GroupAnonymousBot", is_bot=True),
        sender_chat=group(),
    )
    assert not await mw.check(update_with(msg), fake_bot)
    assert replies == []  # Anonymous Telegram users cannot receive a personal response.


# --- level 2: bot admins by user_id ---


def test_admin_by_id_in_allowed_chat():
    settings = make_settings()
    assert is_bot_admin(settings, message("/add", from_user=user(ADMIN_ID)))
    assert not is_bot_admin(settings, message("/add", from_user=user(USER_ID)))


def test_admin_id_in_foreign_chat_is_not_admin():
    assert not is_bot_admin(
        make_settings(), message("/add", chat=group(FOREIGN_CHAT), from_user=user(ADMIN_ID))
    )


def test_sender_chat_is_never_admin_even_with_admin_id():
    settings = make_settings()
    assert not is_bot_admin(
        settings, message("/add", from_user=user(ADMIN_ID), sender_chat=group())
    )
    anon = user(ANON_ADMIN_BOT_ID, "GroupAnonymousBot", is_bot=True)
    assert not is_bot_admin(
        make_settings(bot_admin_ids=frozenset({ANON_ADMIN_BOT_ID})),
        message("/add", from_user=anon, sender_chat=group()),
    )


def test_empty_admin_list_means_no_admins():
    assert not is_bot_admin(make_settings(bot_admin_ids=frozenset()), message("/add"))


def test_callback_admin_check_uses_presser_id():
    settings = make_settings()
    by_admin = CallbackQuery(
        id="1", from_user=user(ADMIN_ID), chat_instance="x", data="hdel:1:y", message=message("x")
    )
    by_user = CallbackQuery(
        id="2",
        from_user=user(USER_ID),
        chat_instance="x",
        data="hdel:1:y",
        message=message("x", from_user=user(ADMIN_ID)),
    )
    assert is_bot_admin(settings, by_admin)
    assert not is_bot_admin(settings, by_user)


async def test_middleware_sets_is_admin_flag(fake_bot):
    mw = SecurityMiddleware(make_settings())
    seen: dict = {}

    async def handler(event, data):
        seen.update(data)

    await mw(handler, update_with(message("/add", from_user=user(ADMIN_ID))), {"bot": fake_bot})
    assert seen["is_admin"] is True
    seen.clear()
    await mw(handler, update_with(message("/add")), {"bot": fake_bot})
    assert seen["is_admin"] is False


async def test_admin_only_middleware_blocks_non_admins(replies):
    mw = AdminOnlyMiddleware()
    called = []

    async def handler(event, data):
        called.append(True)

    await mw(handler, message("/hack_del x"), {"is_admin": False})
    assert called == [] and replies == [ADMIN_ONLY_TEXT]
    await mw(handler, message("/hack_del x"), {})  # missing flag = not admin
    assert called == []
    await mw(handler, message("/hack_del x"), {"is_admin": True})
    assert called == [True]


# --- migration to supergroup ---


async def test_migration_of_allowed_chat_is_followed(fake_bot):
    settings = make_settings()
    calls = []

    async def on_migrate(old, new):
        calls.append((old, new))

    mw = SecurityMiddleware(settings, on_migrate)
    new_chat = ALLOWED_CHAT - 5000
    # The new supergroup receives migrate_from_chat_id (it is not allowed yet).
    msg = message(None, chat=group(new_chat), migrate_from_chat_id=ALLOWED_CHAT)
    assert not await mw.check(update_with(msg), fake_bot)
    # The old group receives migrate_to_chat_id; handled only once.
    msg_old = message(None, chat=group(ALLOWED_CHAT, "group"), migrate_to_chat_id=new_chat)
    assert not await mw.check(update_with(msg_old), fake_bot)
    assert calls == [(ALLOWED_CHAT, new_chat)]
    assert settings.is_chat_allowed(new_chat)
    assert settings.allowed_chat_ids == {new_chat}
    assert not settings.is_chat_allowed(ALLOWED_CHAT)
    assert await mw.check(update_with(message("/help", chat=group(new_chat))), fake_bot)
    assert fake_bot.left == []


async def test_migration_of_foreign_chat_is_ignored(fake_bot):
    settings = make_settings()
    mw = SecurityMiddleware(settings)
    msg = message(None, chat=group(FOREIGN_CHAT - 1), migrate_from_chat_id=FOREIGN_CHAT)
    assert not await mw.check(update_with(msg), fake_bot)
    assert not settings.is_chat_allowed(FOREIGN_CHAT - 1)


def test_command_name_parsing():
    assert command_name(message("/whoami")) == "whoami"
    assert command_name(message("/WhoAmI@hacks_bot extra")) == "whoami"
    assert command_name(message("hello")) is None


async def test_failed_group_migration_does_not_grant_a_new_chat(fake_bot):
    async def fail(old, new):
        raise OSError("database unavailable")

    settings = make_settings()
    middleware = SecurityMiddleware(settings, fail)
    new = ALLOWED_CHAT - 100
    with pytest.raises(OSError):
        await middleware.check(
            update_with(message(None, chat=group(new), migrate_from_chat_id=ALLOWED_CHAT)), fake_bot
        )
    assert settings.allowed_chat_ids == {ALLOWED_CHAT}


async def test_channel_updates_and_inline_callbacks_never_reach_handlers(fake_bot):
    middleware = SecurityMiddleware(make_settings())
    channel = message("/help", chat=Chat(id=ALLOWED_CHAT, type="channel"))
    assert not await middleware.check(update_with(channel), fake_bot)
    inline = CallbackQuery(
        id="inline",
        from_user=user(USER_ID),
        chat_instance="x",
        inline_message_id="id",
        data="my:tasks",
    )
    assert not await middleware.check(Update(update_id=2, callback_query=inline), fake_bot)
    assert fake_bot.sent == []
