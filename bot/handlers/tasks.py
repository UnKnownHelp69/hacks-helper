"""Task commands: /task, /untask, /tasks."""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.context import AppContext
from bot.db.models import Hackathon
from bot.errors import UserError
from bot.handlers.base import parse_issue_number, usage
from bot.handlers.render import task_line
from bot.handlers.views import tasks_view
from bot.respond import respond
from bot.services.args import parse_command, reply_target, resolve_user_token
from bot.services.formatting import issue_link
from bot.services.task_status import STATUS_TITLES

router = Router(name="tasks")

OTHERS_ADMIN_ONLY = "Задачи других назначает и снимает только админ бота"


async def _parse_task_command(
    message: Message, ctx: AppContext, is_admin: bool, usage_text: str
) -> tuple[Hackathon, int, int]:
    """Parse '<hack> [user] <issue>'; without a user: reply target, else the caller."""
    cmd = parse_command(message)
    assert message.from_user is not None
    caller = message.from_user.id
    if len(cmd.tokens) == 3:
        hack = await ctx.hackathon(message.chat.id, cmd.tokens[0].text)
        user_id = await resolve_user_token(
            cmd.tokens[1],
            chat_id=message.chat.id,
            caller_id=caller,
            is_admin=is_admin,
            directory=ctx.directory,
        )
        issue_number = parse_issue_number(cmd.tokens[2].text)
    elif len(cmd.tokens) == 2:
        hack = await ctx.hackathon(message.chat.id, cmd.tokens[0].text)
        issue_number = parse_issue_number(cmd.tokens[1].text)
        target = reply_target(message)
        if target is not None:
            await ctx.directory.remember(target)
            user_id = target.id
        else:
            user_id = caller
    else:
        raise usage(usage_text)
    if user_id != caller and not is_admin:
        raise UserError(OTHERS_ADMIN_ONLY)
    return hack, user_id, issue_number


@router.message(Command("task"))
async def cmd_task(message: Message, ctx: AppContext, is_admin: bool) -> None:
    hack, user_id, number = await _parse_task_command(
        message, ctx, is_admin, "/task <хак> [@user] <номер>"
    )
    result = await ctx.tasks.assign(hack, user_id, number)
    assert result.task is not None
    name = await ctx.directory.plain(message.chat.id, user_id)
    issue = result.issue
    lines = [
        f"📌 {issue_link(issue.number, issue.title, issue.html_url)} → {name} "
        f"({STATUS_TITLES[result.task.status]})"
    ]
    lines.extend(f"⚠️ {w}" for w in result.warnings)
    await respond(message, "\n".join(lines), ctx)


@router.message(Command("untask"))
async def cmd_untask(message: Message, ctx: AppContext, is_admin: bool) -> None:
    hack, user_id, number = await _parse_task_command(
        message, ctx, is_admin, "/untask <хак> [@user] <номер>"
    )
    warnings = await ctx.tasks.unassign(hack, user_id, number)
    name = await ctx.directory.plain(message.chat.id, user_id)
    lines = [f"✅ #{number} снята с {name}"]
    lines.extend(f"⚠️ {w}" for w in warnings)
    await respond(message, "\n".join(lines), ctx)


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, ctx: AppContext, is_admin: bool) -> None:
    cmd = parse_command(message)
    assert message.from_user is not None
    caller = message.from_user.id
    tokens = list(cmd.tokens)
    hack: Hackathon | None = None
    if tokens and tokens[0].ref_kind is None:
        hack = await ctx.hackathon(message.chat.id, tokens.pop(0).text)
    if len(tokens) > 1:
        raise usage("/tasks [<хак>] [@user]")
    user_id: int | None = None
    if tokens:
        user_id = await resolve_user_token(
            tokens[0],
            chat_id=message.chat.id,
            caller_id=caller,
            is_admin=is_admin,
            directory=ctx.directory,
        )
    elif (target := reply_target(message)) is not None:
        user_id = target.id
    elif hack is None:
        user_id = caller

    if hack is not None and user_id is None:
        await respond(message, await _hackathon_tasks(ctx, hack), ctx)
        return
    assert user_id is not None
    text = await tasks_view(ctx, message.chat.id, user_id, hack)
    # "/tasks" alone: a reusable "my tasks" button computed for whoever presses it.
    personal = "tasks" if user_id == caller and hack is None and not tokens else None
    await respond(message, text, ctx, personal=personal)


async def _hackathon_tasks(ctx: AppContext, hack: Hackathon) -> str:
    tasks = await ctx.storage.tasks.active_for_hackathon(hack.id)
    if not tasks:
        return f"📝 {escape(hack.slug)}: задач нет"
    grouped: dict[int, list] = {}
    for task in tasks:
        grouped.setdefault(task.user_id, []).append(task)
    lines = [f"📝 <b>{escape(hack.slug)}</b>: задач {len(tasks)}"]
    for user_id, user_tasks in grouped.items():
        lines.append(f"\n<b>{await ctx.directory.plain(hack.chat_id, user_id)}</b>")
        lines.extend(task_line(t) for t in user_tasks)
    return "\n".join(lines)
