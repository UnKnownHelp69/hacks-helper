"""User identification: registry, @username/reply/text_mention/id resolution, display."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import MessageEntity

from bot.errors import UserError
from bot.services.args import parse_command, reply_target, resolve_user_token, resolve_user_tokens
from bot.services.availability import parse_day_value, serialize_day
from bot.services.notify import Pinger
from bot.services.users import UserDirectory
from tests.conftest import ALLOWED_CHAT, OTHER_ID, USER_ID, message, user

MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture
async def directory(storage, fake_bot):
    return UserDirectory(fake_bot, storage.users, ttl_seconds=300)


def entity(kind: str, text: str, part: str, **kw) -> MessageEntity:
    """Entity covering ``part`` inside ``text`` (offsets in UTF-16 code units)."""
    start = text.index(part)
    offset = len(text[:start].encode("utf-16-le")) // 2
    length = len(part.encode("utf-16-le")) // 2
    return MessageEntity(type=kind, offset=offset, length=length, **kw)


# --- tokenizer ---


def test_parse_command_with_text_mention_after_emoji():
    target = user(OTHER_ID, None, "Иван", last_name="Петров")
    text = "/add hack 🚀 Иван Петров @bobby"
    msg = message(
        text,
        entities=[
            entity("bot_command", text, "/add"),
            entity("text_mention", text, "Иван Петров", user=target),
            entity("mention", text, "@bobby"),
        ],
    )
    tokens = parse_command(msg).tokens
    assert [t.text for t in tokens] == ["hack", "🚀", "Иван Петров", "@bobby"]
    assert tokens[2].user is not None and tokens[2].user.id == OTHER_ID
    assert tokens[2].ref_kind == "text_mention"
    assert tokens[3].ref_kind == "username"
    assert tokens[1].ref_kind is None


def test_parse_command_quotes_and_rest():
    cmd = parse_command(message('/issues hack label:"in progress"'))
    assert [t.text for t in cmd.tokens] == ["hack", "label:in progress"]
    cmd = parse_command(message("/issue_new hack Fix login\nDetails here"))
    assert cmd.rest(1) == "Fix login\nDetails here"


def test_command_with_bot_mention():
    cmd = parse_command(message("/left@hacks_bot hack"))
    assert [t.text for t in cmd.tokens] == ["hack"]


def test_ref_kinds():
    tokens = parse_command(message("/x me я 123456789 12 #12 word")).tokens
    assert [t.ref_kind for t in tokens] == ["self", "self", "id", None, None, None]


def test_reply_target_ignores_bots_and_chats():
    replied = message("hi", from_user=user(OTHER_ID, "bobby"))
    assert reply_target(message("/task h 1", reply_to_message=replied)).id == OTHER_ID
    bot_msg = message("hi", from_user=user(1, "somebot", is_bot=True))
    assert reply_target(message("/task h 1", reply_to_message=bot_msg)) is None
    assert reply_target(message("/task h 1")) is None


# --- @username resolution ---


async def test_unknown_username(directory):
    token = parse_command(message("/x @ghost")).tokens[0]
    with pytest.raises(UserError, match="Я ещё не знаю @ghost"):
        await resolve_user_token(
            token, chat_id=ALLOWED_CHAT, caller_id=USER_ID, is_admin=False, directory=directory
        )


async def test_known_username_verified_via_get_chat_member(directory, storage, fake_bot):
    bob = user(OTHER_ID, "Bobby")
    await storage.users.upsert(OTHER_ID, "Bobby", "Bob", None)
    fake_bot.set_member(ALLOWED_CHAT, bob)
    token = parse_command(message("/x @bobby")).tokens[0]  # case-insensitive
    assert (
        await resolve_user_token(
            token, chat_id=ALLOWED_CHAT, caller_id=USER_ID, is_admin=False, directory=directory
        )
        == OTHER_ID
    )
    assert fake_bot.get_member_calls == 1


async def test_username_changed_since_registry(directory, storage, fake_bot):
    await storage.users.upsert(OTHER_ID, "oldnick", "Bob", None)
    fake_bot.set_member(ALLOWED_CHAT, user(OTHER_ID, "newnick", "Bob"))
    token = parse_command(message("/x @oldnick")).tokens[0]
    with pytest.raises(UserError, match="никому из известных участников не принадлежит"):
        await resolve_user_token(
            token, chat_id=ALLOWED_CHAT, caller_id=USER_ID, is_admin=False, directory=directory
        )
    # the registry now has the actual username
    assert (await storage.users.get(OTHER_ID)).username == "newnick"
    assert await storage.users.find_by_username("oldnick") is None


async def test_username_falls_back_to_registry_when_telegram_down(directory, storage, fake_bot):
    await storage.users.upsert(OTHER_ID, "bobby", "Bob", None)
    fake_bot.network_down = True
    token = parse_command(message("/x @bobby")).tokens[0]
    assert (
        await resolve_user_token(
            token, chat_id=ALLOWED_CHAT, caller_id=USER_ID, is_admin=False, directory=directory
        )
        == OTHER_ID
    )


async def test_text_mention_registers_user(directory, storage):
    target = user(OTHER_ID, None, "Иван")
    text = "/add hack Иван"
    msg = message(
        text,
        entities=[
            entity("bot_command", text, "/add"),
            entity("text_mention", text, "Иван", user=target),
        ],
    )
    token = parse_command(msg).tokens[1]
    assert (
        await resolve_user_token(
            token, chat_id=ALLOWED_CHAT, caller_id=USER_ID, is_admin=False, directory=directory
        )
        == OTHER_ID
    )
    assert (await storage.users.get(OTHER_ID)).first_name == "Иван"


async def test_numeric_id_only_for_admins(directory):
    token = parse_command(message(f"/x {OTHER_ID}0000")).tokens[0]
    with pytest.raises(UserError, match="только администраторы"):
        await resolve_user_token(
            token, chat_id=ALLOWED_CHAT, caller_id=USER_ID, is_admin=False, directory=directory
        )
    assert (
        await resolve_user_token(
            token, chat_id=ALLOWED_CHAT, caller_id=USER_ID, is_admin=True, directory=directory
        )
        == OTHER_ID * 10000
    )


async def test_partial_resolution_reports_unknown(directory, storage, fake_bot):
    await storage.users.upsert(OTHER_ID, "bobby", "Bob", None)
    fake_bot.set_member(ALLOWED_CHAT, user(OTHER_ID, "bobby"))
    tokens = parse_command(message("/add h @bobby @ghost me")).tokens[1:]
    ids, errors = await resolve_user_tokens(
        tokens, chat_id=ALLOWED_CHAT, caller_id=USER_ID, is_admin=True, directory=directory
    )
    assert ids == [OTHER_ID, USER_ID]
    assert len(errors) == 1 and "@ghost" in errors[0]


# --- display ---


async def test_display_uses_current_username_and_cache(directory, storage, fake_bot):
    await storage.users.upsert(OTHER_ID, "old", "Bob", None)
    fake_bot.set_member(ALLOWED_CHAT, user(OTHER_ID, "fresh", "Bob"))
    assert await directory.plain(ALLOWED_CHAT, OTHER_ID) == "fresh"
    info = await directory.lookup(ALLOWED_CHAT, OTHER_ID)
    assert info.mention == "@fresh"
    assert fake_bot.get_member_calls == 1  # cached


async def test_display_without_username_uses_tg_link(directory, fake_bot):
    fake_bot.set_member(ALLOWED_CHAT, user(OTHER_ID, None, "Иван <script>"))
    info = await directory.lookup(ALLOWED_CHAT, OTHER_ID)
    assert info.mention == f'<a href="tg://user?id={OTHER_ID}">Иван &lt;script&gt;</a>'
    assert info.plain == "Иван &lt;script&gt;"
    assert "@" not in info.plain


async def test_display_falls_back_to_registry(directory, storage, fake_bot):
    await storage.users.upsert(OTHER_ID, "cached", "Bob", None)
    fake_bot.network_down = True
    info = await directory.lookup(ALLOWED_CHAT, OTHER_ID)
    assert info.username == "cached" and info.in_group is None


# --- pings: membership and availability ---


async def test_private_chat_pings_only_its_owner(directory, storage, fake_bot):
    await directory.remember(user(USER_ID, "owner"))
    await directory.remember(user(OTHER_ID, "other"))
    now = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    pinger = Pinger(directory, storage.availability)
    plan = await pinger.plan(USER_ID, [USER_ID, OTHER_ID], tz=MSK, now=now)
    assert plan.mentions == ["@owner"]
    assert plan.not_in_group == ["other"]
    assert fake_bot.get_member_calls == 0

    await storage.availability.set_days(USER_ID, {0: serialize_day(parse_day_value("нет"))})
    plan = await pinger.plan(USER_ID, [USER_ID], tz=MSK, now=now)
    assert plan.mentions == [] and plan.skipped_names == ["owner"]


async def test_ping_plan_skips_left_and_defers_unavailable(directory, storage, fake_bot):
    fake_bot.set_member(ALLOWED_CHAT, user(1, "here"))
    fake_bot.set_member(ALLOWED_CHAT, user(2, "gone"), "left")
    fake_bot.set_member(ALLOWED_CHAT, user(3, "banned"), "kicked")
    fake_bot.set_member(ALLOWED_CHAT, user(4, "sleepy"))
    fake_bot.set_member(ALLOWED_CHAT, user(5, "never"))
    # Monday 2026-10-26 03:00 MSK: user 4 available from 09:00, user 5 never on Monday
    await storage.availability.set_days(4, {0: serialize_day(parse_day_value("09:00-21:00"))})
    await storage.availability.set_days(
        5, {d: serialize_day(parse_day_value("нет")) for d in range(7)}
    )
    now = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    deadline = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    plan = await Pinger(directory, storage.availability).plan(
        ALLOWED_CHAT, [1, 2, 3, 4, 5], tz=MSK, now=now, deadline=deadline
    )
    assert plan.mentions == ["@here"]
    assert plan.not_in_group == ["gone", "banned"]
    assert plan.deferred == {4: datetime(2026, 10, 26, 9, 0, tzinfo=MSK)}
    assert plan.skipped_names == ["never"]
    rendered = "\n".join(plan.render_lines())
    assert "gone нет в группе" in rendered
    assert "@gone" not in rendered and "@sleepy" not in rendered


async def test_window_after_deadline_skips_ping(directory, storage, fake_bot):
    fake_bot.set_member(ALLOWED_CHAT, user(4, "sleepy"))
    await storage.availability.set_days(4, {0: serialize_day(parse_day_value("09:00-21:00"))})
    now = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)  # 03:00 MSK
    deadline = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)  # 08:00 MSK, before the window
    plan = await Pinger(directory, storage.availability).plan(
        ALLOWED_CHAT, [4], tz=MSK, now=now, deadline=deadline
    )
    assert plan.deferred == {} and plan.skipped_names == ["sleepy"]
