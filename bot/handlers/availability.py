"""Availability schedule commands: /avail, /avail_set, /avail_reset."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.context import AppContext
from bot.errors import UserError
from bot.handlers.base import usage
from bot.services.args import Token, parse_command, reply_target, resolve_user_token
from bot.services.availability import (
    WEEKDAY_NAMES,
    describe_schedule,
    deserialize_schedule,
    parse_day_value,
    parse_days,
    serialize_day,
)
from bot.services.timeutil import tz_label

router = Router(name="availability")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

OTHERS_ADMIN_ONLY = "Менять расписание другого человека может только администратор бота"


async def _target(message: Message, ctx: AppContext, is_admin: bool, token: Token | None) -> int:
    assert message.from_user is not None
    caller = message.from_user.id
    if token is not None:
        user_id = await resolve_user_token(
            token,
            chat_id=message.chat.id,
            caller_id=caller,
            is_admin=is_admin,
            directory=ctx.directory,
        )
    elif is_admin and (target := reply_target(message)) is not None:
        await ctx.directory.remember(target)
        user_id = target.id
    else:
        user_id = caller
    return user_id


async def _show(message: Message, ctx: AppContext, user_id: int, prefix: str = "") -> None:
    schedule = deserialize_schedule(await ctx.storage.availability.get_raw(user_id))
    tz = await ctx.zone(message.chat.id)
    name = await ctx.directory.plain(message.chat.id, user_id)
    note = " (по умолчанию: можно всегда)" if schedule.is_default else ""
    await message.reply(
        f"{prefix}🕒 Когда можно пинговать {name}{note}, время {tz_label(tz)}:\n"
        f"{describe_schedule(schedule)}"
    )


@router.message(Command("avail"))
async def cmd_avail(message: Message, ctx: AppContext, is_admin: bool) -> None:
    cmd = parse_command(message)
    if len(cmd.tokens) > 1:
        raise usage("/avail [@user]")
    assert message.from_user is not None
    if cmd.tokens:
        user_id = await resolve_user_token(
            cmd.tokens[0],
            chat_id=message.chat.id,
            caller_id=message.from_user.id,
            is_admin=is_admin,
            directory=ctx.directory,
        )
    elif (target := reply_target(message)) is not None:
        user_id = target.id
    else:
        user_id = message.from_user.id
    await _show(message, ctx, user_id)


@router.message(Command("avail_set"))
async def cmd_avail_set(message: Message, ctx: AppContext, is_admin: bool) -> None:
    cmd = parse_command(message)
    tokens = list(cmd.tokens)
    user_token = tokens.pop() if tokens and tokens[-1].is_explicit_user else None
    if len(tokens) < 2:
        raise usage("/avail_set <дни> <да|нет|09:00-21:00[,22:00-02:00]> [@user]")
    assert message.from_user is not None
    user_id = await _target(message, ctx, is_admin, user_token)
    if user_id != message.from_user.id and not is_admin:
        raise UserError(OTHERS_ADMIN_ONLY)
    days = parse_days(tokens[0].text)
    value = parse_day_value("".join(t.text for t in tokens[1:]))
    await ctx.storage.availability.set_days(user_id, {day: serialize_day(value) for day in days})
    changed = ", ".join(WEEKDAY_NAMES[d] for d in days)
    await _show(message, ctx, user_id, prefix=f"✅ Обновлено: {changed} — {value.describe()}\n")


@router.message(Command("avail_reset"))
async def cmd_avail_reset(message: Message, ctx: AppContext, is_admin: bool) -> None:
    cmd = parse_command(message)
    if len(cmd.tokens) > 1:
        raise usage("/avail_reset [@user]")
    assert message.from_user is not None
    user_id = await _target(message, ctx, is_admin, cmd.tokens[0] if cmd.tokens else None)
    if user_id != message.from_user.id and not is_admin:
        raise UserError(OTHERS_ADMIN_ONLY)
    await ctx.storage.availability.reset(user_id)
    await _show(message, ctx, user_id, prefix="✅ Расписание сброшено.\n")
