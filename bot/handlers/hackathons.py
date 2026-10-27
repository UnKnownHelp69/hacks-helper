"""Hackathon list and cards through /hack."""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.context import AppContext
from bot.db.database import utcnow
from bot.db.models import HACK_ACTIVE
from bot.handlers.base import usage
from bot.handlers.render import hackathon_card, left_text, members_text
from bot.respond import respond
from bot.services.args import parse_command
from bot.services.timeutil import format_dt

router = Router(name="hackathons")


@router.message(Command("hack"))
async def cmd_hack(message: Message, ctx: AppContext) -> None:
    tokens = parse_command(message).tokens
    if len(tokens) > 1:
        raise usage("/hack [название]")
    if tokens:
        hack = await ctx.hackathon(message.chat.id, tokens[0].text)
        await respond(message, await hackathon_card(ctx, hack, utcnow()), ctx)
        return

    hacks = await ctx.storage.hackathons.list(message.chat.id, HACK_ACTIVE)
    if not hacks:
        await respond(message, "Активных хакатонов нет", ctx)
        return
    tz = await ctx.zone(message.chat.id)
    now = utcnow()
    blocks = []
    for hack in hacks:
        blocks.append(
            f"🏆 <b>{escape(hack.display_name)}</b>\n"
            f"Рег. до {format_dt(hack.registration_end, tz)} · "
            f"{format_dt(hack.start_at, tz)} → {format_dt(hack.end_at, tz)}\n"
            f"{left_text(hack, now, tz, with_slug=False).split(chr(10))[0]}\n"
            f"Участники: {await members_text(ctx, hack)}"
        )
    await respond(message, "\n\n".join(blocks), ctx)
