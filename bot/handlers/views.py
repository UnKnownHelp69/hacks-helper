"""Personal views: computed for whoever asks (command author or button presser)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from html import escape

from aiogram.types import User

from bot.context import AppContext
from bot.db.models import HACK_ACTIVE, Hackathon
from bot.handlers.render import task_line
from bot.services.availability import describe_schedule, deserialize_schedule
from bot.services.timeutil import tz_label


async def me_view(ctx: AppContext, chat_id: int, user: User) -> str:
    await ctx.directory.remember(user)
    ctx.directory.invalidate(chat_id, user.id)
    login = await ctx.storage.github_links.get_login(user.id)
    name = escape(user.username or user.full_name)
    return f"👤 {name}\nid: {user.id}\nGitHub: {escape(login) if login else 'не привязан'}"


async def tasks_view(
    ctx: AppContext, chat_id: int, user_id: int, hack: Hackathon | None = None
) -> str:
    hacks = [hack] if hack else await ctx.storage.hackathons.list(chat_id, HACK_ACTIVE)
    by_id = {h.id: h for h in hacks}
    tasks = await ctx.storage.tasks.active_for_user(user_id, list(by_id))
    name = await ctx.directory.plain(chat_id, user_id)
    scope = f" [{escape(hack.slug)}]" if hack else ""
    if not tasks:
        return f"📝 {name}{scope}: задач нет"
    lines = [f"📝 {name}{scope}:"]
    for task in tasks:
        lines.append(task_line(task, with_hack=None if hack else by_id[task.hackathon_id].slug))
    return "\n".join(lines)


async def avail_view(ctx: AppContext, chat_id: int, user_id: int) -> str:
    schedule = deserialize_schedule(await ctx.storage.availability.get_raw(user_id))
    name = await ctx.directory.plain(chat_id, user_id)
    if schedule.is_default:
        return f"🕒 {name}: можно всегда"
    tz = await ctx.zone(chat_id)
    return f"🕒 {name} ({escape(tz_label(tz))}):\n{describe_schedule(schedule)}"


async def _my_tasks(ctx: AppContext, chat_id: int, user: User) -> str:
    return await tasks_view(ctx, chat_id, user.id)


async def _my_avail(ctx: AppContext, chat_id: int, user: User) -> str:
    return await avail_view(ctx, chat_id, user.id)


PERSONAL_VIEWS: dict[str, Callable[[AppContext, int, User], Awaitable[str]]] = {
    "me": me_view,
    "tasks": _my_tasks,
    "avail": _my_avail,
}
