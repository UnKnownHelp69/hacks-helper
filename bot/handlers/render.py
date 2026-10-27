"""Rendering of hackathons and tasks for chat output (no pings in lists)."""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from bot.context import AppContext
from bot.db.models import Hackathon, Task
from bot.services.formatting import issue_link, link
from bot.services.schedule_logic import MILESTONE_UNTIL, current_phase, next_milestone
from bot.services.task_status import STATUS_ICONS, STATUS_TITLES
from bot.services.timeutil import format_dt, format_duration, tz_label


def place_text(hack: Hackathon) -> str:
    if hack.place is None:
        return "не указано"
    if hack.place == 0:
        return "без места"
    return f"{hack.place}"


def repo_text(hack: Hackathon) -> str:
    if not hack.repo:
        return "не указан"
    return link(f"https://github.com/{hack.repo}", hack.repo)


def dates_block(hack: Hackathon, tz: ZoneInfo) -> str:
    return (
        f"Регистрация до: {format_dt(hack.registration_end, tz)}\n"
        f"Начало: {format_dt(hack.start_at, tz)}\n"
        f"Конец: {format_dt(hack.end_at, tz)} ({escape(tz_label(tz))})"
    )


def left_text(hack: Hackathon, now: datetime, tz: ZoneInfo, *, with_slug: bool = True) -> str:
    nxt = next_milestone(hack, now)
    prefix = f"<b>{escape(hack.slug)}</b>: " if with_slug else ""
    if nxt is None:
        return f"🏁 {prefix}хакатон завершён"
    until = MILESTONE_UNTIL[nxt.milestone]
    return (
        f"⏳ {prefix}{until if with_slug else until.capitalize()} осталось "
        f"<b>{format_duration(nxt.remaining)}</b> "
        f"({format_dt(nxt.at, tz)} {escape(tz_label(tz))})\n"
        f"Сейчас: {current_phase(hack, now)}"
    )


async def members_text(ctx: AppContext, hack: Hackathon) -> str:
    members = await ctx.storage.hackathons.members(hack.id)
    if not members:
        return "нет"
    names = await ctx.directory.plain_many(hack.chat_id, members)
    return ", ".join(names)


async def hackathon_card(ctx: AppContext, hack: Hackathon, now: datetime) -> str:
    tz = await ctx.zone(hack.chat_id)
    status = "активен" if hack.is_active else "в архиве"
    lines = [
        f"🏆 <b>{escape(hack.display_name)}</b> — {status}",
        dates_block(hack, tz),
    ]
    if hack.is_active:
        lines.append(left_text(hack, now, tz, with_slug=False))
    else:
        lines.append(f"Место: {place_text(hack)}")
    members = await ctx.storage.hackathons.members(hack.id)
    lines.append(f"Репо: {repo_text(hack)}")
    lines.append(f"Участники ({len(members)}): {await members_text(ctx, hack)}")
    materials = materials_lines(hack)
    if materials:
        lines.extend(materials)
    return "\n".join(lines)


def materials_lines(hack: Hackathon) -> list[str]:
    lines = []
    if hack.site_url:
        lines.append(f"🌐 Сайт: {link(hack.site_url, hack.site_url)}")
    if hack.org_chat_url:
        lines.append(f"💬 Чат организаторов: {link(hack.org_chat_url, hack.org_chat_url)}")
    if hack.spec_url:
        lines.append(f"📄 ТЗ: {link(hack.spec_url, hack.spec_url)}")
    elif hack.spec_file_id:
        name = hack.spec_file_name or "файл"
        lines.append(f"📄 ТЗ: файл «{escape(name)}»")
    return lines


def task_line(task: Task, *, with_hack: str | None = None) -> str:
    prefix = f"[{escape(with_hack)}] " if with_hack else ""
    return (
        f"{STATUS_ICONS.get(task.status, '•')} {prefix}"
        f"{issue_link(task.issue_number, task.issue_title, task.issue_url)} — "
        f"{STATUS_TITLES.get(task.status, task.status)}"
    )
