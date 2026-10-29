"""Personal issue assignment and task views inside /hack."""

from __future__ import annotations

from functools import partial
from html import escape

from aiogram.types import CallbackQuery, Message

from bot.context import AppContext
from bot.db.models import HackSession
from bot.errors import UserError
from bot.handlers.interactive import keyboard, session_button
from bot.handlers.views import tasks_view
from bot.respond import personal_reply
from bot.services.formatting import issue_link, split_message
from bot.services.tasks import require_repo

Event = Message | CallbackQuery
button = partial(session_button, "hk")


async def show_tasks(
    event: Event, ctx: AppContext, session: HackSession, page: int = 0, notice: str = ""
) -> None:
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    text = await tasks_view(ctx, session.chat_id, session.user_id, hack)
    if notice:
        text = notice + "\n\n" + text
    if not ctx.github.configured:
        text += "\n\nGitHub не подключён. Сохранённые задачи доступны; назначение пока недоступно."
    chunks = split_message(text, limit=3000)
    page = min(max(page, 0), len(chunks) - 1)
    session.step = "tasks"
    await ctx.storage.workflow.save_session(session)
    rows = [[button(session, "В меню", "menu")]]
    if hack.is_active and hack.repo and ctx.github.configured:
        rows.append([button(session, "📌 Назначить issue", "task_issues", 0)])
    navigation = []
    if page:
        navigation.append(button(session, "←", "task_page", page - 1))
    if page + 1 < len(chunks):
        navigation.append(button(session, "→", "task_page", page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([button(session, "К карточке", "card")])
    await personal_reply(event, chunks[page], reply_markup=keyboard(rows))


async def choose_issue(event: Event, ctx: AppContext, session: HackSession, page: int = 0) -> None:
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    if not hack.is_active:
        raise UserError("Хакатон в архиве")
    repo = require_repo(hack)
    issues = await ctx.github.list_open_issues(repo)
    session.step = "task:issues"
    session.data.update(_task_repo=repo, _task_issues=[issue.number for issue in issues])
    session.data.pop("_task_issue", None)
    await ctx.storage.workflow.save_session(session)
    page = min(max(page, 0), max(0, (len(issues) - 1) // 8))
    rows = [[button(session, "В меню", "menu")]]
    rows.extend(
        [button(session, f"#{issue.number} · {issue.title}"[:60], "task_issue", issue.number)]
        for issue in issues[page * 8 : (page + 1) * 8]
    )
    navigation = []
    if page:
        navigation.append(button(session, "←", "task_issues", page - 1))
    if (page + 1) * 8 < len(issues):
        navigation.append(button(session, "→", "task_issues", page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([button(session, "К задачам", "tasks")])
    await personal_reply(
        event,
        f"🐙 <b>{escape(repo)}</b>\n" + ("Выберите issue." if issues else "Открытых issues нет."),
        reply_markup=keyboard(rows),
    )


async def choose_assignee(
    event: Event, ctx: AppContext, session: HackSession, page: int = 0
) -> None:
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    if hack.repo != session.data.get("_task_repo"):
        raise UserError("Репозиторий изменился. Откройте список задач заново")
    number = int(session.data["_task_issue"])
    issue = await ctx.github.get_issue(require_repo(hack), number)
    if issue.state == "closed":
        raise UserError(f"#{number} уже закрыт")
    members = []
    for user_id in await ctx.storage.hackathons.members(hack.id):
        info = await ctx.directory.lookup(hack.chat_id, user_id)
        if info.in_group is True:
            members.append((user_id, "@" + info.username if info.username else info.full_name))
    session.step = "task:assignee"
    await ctx.storage.workflow.save_session(session)
    page = min(max(page, 0), max(0, (len(members) - 1) // 8))
    rows = [[button(session, "В меню", "menu")]]
    rows.extend(
        [button(session, name[:60], "task_assign", user_id)]
        for user_id, name in members[page * 8 : (page + 1) * 8]
    )
    navigation = []
    if page:
        navigation.append(button(session, "←", "task_members", page - 1))
    if (page + 1) * 8 < len(members):
        navigation.append(button(session, "→", "task_members", page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([button(session, "К issues", "task_issues", 0)])
    await personal_reply(
        event,
        issue_link(issue.number, issue.title, issue.html_url)
        + "\n"
        + ("Выберите исполнителя." if members else "Сначала зарегистрируйтесь на хакатон."),
        reply_markup=keyboard(rows),
    )


async def assign_issue(event: Event, ctx: AppContext, session: HackSession, user_id: int) -> None:
    if session.step != "task:assignee":
        raise UserError("Сначала выберите issue")
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    if hack.repo != session.data.get("_task_repo"):
        raise UserError("Репозиторий изменился. Откройте список задач заново")
    info = await ctx.directory.lookup(hack.chat_id, user_id, force=True)
    if info.in_group is not True:
        raise UserError("Не удалось подтвердить, что исполнитель участвует в этом чате")
    result = await ctx.tasks.assign(hack, user_id, int(session.data["_task_issue"]))
    notice = (
        f"📌 {issue_link(result.issue.number, result.issue.title, result.issue.html_url)} "
        f"→ {info.mention}"
    )
    if result.warnings:
        notice += "\n" + "\n".join(f"⚠️ {warning}" for warning in result.warnings)
    await show_tasks(event, ctx, session, notice=notice)
