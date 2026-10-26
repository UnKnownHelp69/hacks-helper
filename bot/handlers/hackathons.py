"""Viewing hackathons: /hacks, /hack, /left, /links, /history, /stats."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.context import AppContext
from bot.db.database import utcnow
from bot.db.models import HACK_ACTIVE, HACK_FINISHED
from bot.handlers.base import NO_PREVIEW, reply, usage
from bot.handlers.render import (
    hackathon_card,
    left_text,
    materials_lines,
    members_text,
    place_text,
    repo_text,
)
from bot.services.args import parse_command
from bot.services.timeutil import format_dt

router = Router(name="hackathons")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


def _single_arg(message: Message, usage_text: str) -> str:
    words = [t.text for t in parse_command(message).tokens]
    if len(words) != 1:
        raise usage(usage_text)
    return words[0]


@router.message(Command("hacks"))
async def cmd_hacks(message: Message, ctx: AppContext) -> None:
    hacks = await ctx.storage.hackathons.list(message.chat.id, HACK_ACTIVE)
    if not hacks:
        await message.reply("Активных хакатонов нет. Создать: /hack_new (🔒), архив: /history")
        return
    tz = await ctx.zone(message.chat.id)
    now = utcnow()
    blocks = []
    for hack in hacks:
        blocks.append(
            f"🏆 <b>{escape(hack.display_name)}</b>\n"
            f"Рег. до {format_dt(hack.registration_end, tz)} · "
            f"{format_dt(hack.start_at, tz)} → {format_dt(hack.end_at, tz)}\n"
            f"{left_text(hack, now, tz).split(chr(10))[0]}\n"
            f"Участники: {await members_text(ctx, hack)}"
        )
    await reply(message, "\n\n".join(blocks) + "\n\nПодробнее: /hack &lt;хак&gt;")


@router.message(Command("hack"))
async def cmd_hack(message: Message, ctx: AppContext) -> None:
    hack = await ctx.hackathon(message.chat.id, _single_arg(message, "/hack <хак>"))
    await reply(message, await hackathon_card(ctx, hack, utcnow()))


@router.message(Command("left"))
async def cmd_left(message: Message, ctx: AppContext) -> None:
    hack = await ctx.hackathon(message.chat.id, _single_arg(message, "/left <хак>"))
    tz = await ctx.zone(message.chat.id)
    await message.reply(left_text(hack, utcnow(), tz))


@router.message(Command("links"))
async def cmd_links(message: Message, ctx: AppContext) -> None:
    hack = await ctx.hackathon(message.chat.id, _single_arg(message, "/links <хак>"))
    lines = [f"📎 <b>{escape(hack.display_name)}</b>", *materials_lines(hack)]
    if len(lines) == 1:
        lines.append("Ссылки и ТЗ ещё не заданы (🔒 /site, /orgchat, /spec)")
    text = "\n".join(lines)
    if hack.spec_file_id and not hack.spec_url:
        # One message: the spec file with every link in the caption.
        await message.reply_document(hack.spec_file_id, caption=text[:1024])
        return
    await message.reply(text, link_preview_options=NO_PREVIEW)


@router.message(Command("history"))
async def cmd_history(message: Message, ctx: AppContext) -> None:
    words = [t.text for t in parse_command(message).tokens]
    tz = await ctx.zone(message.chat.id)
    if words:
        hack = await ctx.hackathon(message.chat.id, words[0])
        total, per_user = await ctx.storage.tasks.closed_counts(hack.id)
        members = await ctx.storage.hackathons.members(hack.id)
        status = "в архиве" if hack.status == HACK_FINISHED else "ещё идёт"
        lines = [
            f"📚 <b>{escape(hack.display_name)}</b> — {status}",
            f"Даты: рег. до {format_dt(hack.registration_end, tz)}, "
            f"{format_dt(hack.start_at, tz)} → {format_dt(hack.end_at, tz)}",
            f"Репозиторий: {repo_text(hack)}",
            f"Место: {place_text(hack)}",
            f"Закрыто issues: {total}",
            "Участники (закрыто issues):",
        ]
        for user_id in members:
            name = await ctx.directory.plain(message.chat.id, user_id)
            lines.append(f"• {name} — {per_user.get(user_id, 0)}")
        for user_id, count in per_user.items():
            if user_id not in members:
                name = await ctx.directory.plain(message.chat.id, user_id)
                lines.append(f"• {name} (уже не в составе) — {count}")
        if not members and not per_user:
            lines.append("• нет")
        await reply(message, "\n".join(lines))
        return

    hacks = await ctx.storage.hackathons.list(message.chat.id, HACK_FINISHED)
    if not hacks:
        await message.reply("Архив пуст: завершённых хакатонов ещё нет")
        return
    lines = ["📚 <b>Завершённые хакатоны</b>"]
    for hack in sorted(hacks, key=lambda h: h.end_at, reverse=True):
        total, _ = await ctx.storage.tasks.closed_counts(hack.id)
        members = await ctx.storage.hackathons.members(hack.id)
        lines.append(
            f"\n<b>{escape(hack.display_name)}</b> · {hack.start_at.astimezone(tz):%d.%m.%Y}"
            f"–{hack.end_at.astimezone(tz):%d.%m.%Y}\n"
            f"Место: {place_text(hack)} · закрыто issues: {total} · участников: {len(members)}\n"
            f"Репозиторий: {repo_text(hack)}\n"
            f"Участники: {await members_text(ctx, hack)}"
        )
    lines.append("\nПодробно: /history &lt;хак&gt;")
    await reply(message, "\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: AppContext) -> None:
    counts = await ctx.storage.hackathons.participation_counts(message.chat.id)
    closed = await ctx.storage.tasks.closed_counts_by_user(message.chat.id)
    user_ids = set(counts) | set(closed)
    if not user_ids:
        await message.reply("Статистики пока нет")
        return
    rows = []
    for user_id in user_ids:
        finished, total = counts.get(user_id, (0, 0))
        rows.append((finished, total, closed.get(user_id, 0), user_id))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    lines = ["📊 <b>Статистика участников</b>", "хакатонов завершено (всего) · закрыто issues"]
    for finished, total, closed_count, user_id in rows:
        name = await ctx.directory.plain(message.chat.id, user_id)
        lines.append(f"• {name}: {finished} ({total}) · {closed_count}")
    await reply(message, "\n".join(lines))
