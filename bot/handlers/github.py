"""GitHub commands: /issues, /issue_new, /link_github, /unlink_github."""

from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.context import AppContext, IssueQuery
from bot.errors import UserError
from bot.handlers.base import NO_PREVIEW, reply, usage
from bot.services.args import Token, parse_command, reply_target, resolve_user_token
from bot.services.formatting import issue_link
from bot.services.github import Issue, normalize_login
from bot.services.tasks import require_repo

log = logging.getLogger(__name__)

router = Router(name="github")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

PAGE_SIZE = 20


async def _target_user(
    message: Message, ctx: AppContext, is_admin: bool, tokens: list[Token]
) -> int | None:
    """Explicit user token (first one that is a user reference) or None."""
    assert message.from_user is not None
    for token in tokens:
        if token.ref_kind is not None:
            return await resolve_user_token(
                token,
                chat_id=message.chat.id,
                caller_id=message.from_user.id,
                is_admin=is_admin,
                directory=ctx.directory,
            )
    return None


async def _render_issues_page(
    ctx: AppContext, query: IssueQuery, page: int, token: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    hack = await ctx.storage.hackathons.get_by_id(query.hackathon_id)
    if hack is None:
        raise UserError("Хакатон не найден")
    repo = require_repo(hack)
    issues: list[Issue] = await ctx.github.list_open_issues(
        repo, assignee=query.assignee_login, label=query.label
    )
    pages = max(1, (len(issues) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    filters = []
    if query.assignee_login:
        filters.append(f"исполнитель {escape(query.assignee_login)}")
    if query.label:
        filters.append(f"метка «{escape(query.label)}»")
    header = (
        f"📋 <b>{escape(hack.slug)}</b> · <code>{escape(repo)}</code> · открытых: {len(issues)}"
    )
    if filters:
        header += f" ({', '.join(filters)})"
    if not issues:
        return header + "\nОткрытых issues нет", None

    # GitHub login -> Telegram display (without pings)
    members = await ctx.storage.hackathons.members(hack.id)
    links = await ctx.storage.github_links.logins_for(members)
    by_login = {login.lower(): uid for uid, login in links.items()}

    lines = [header]
    for issue in issues[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        assignees = []
        for login in issue.assignees:
            uid = by_login.get(login.lower())
            if uid is not None:
                assignees.append(await ctx.directory.plain(hack.chat_id, uid))
            else:
                assignees.append(escape(login))
        who = ", ".join(assignees) if assignees else "—"
        labels = f" [{escape(', '.join(issue.labels))}]" if issue.labels else ""
        lines.append(f"• {issue_link(issue.number, issue.title, issue.html_url)}{labels} — {who}")
    if pages > 1:
        lines.append(f"Стр. {page + 1}/{pages}")
        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"iss:{token}:{page - 1}"))
        if page < pages - 1:
            buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"iss:{token}:{page + 1}"))
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[buttons])
    return "\n".join(lines), None


@router.message(Command("issues"))
async def cmd_issues(message: Message, ctx: AppContext, is_admin: bool) -> None:
    cmd = parse_command(message)
    if not cmd.tokens:
        raise usage("/issues <хак> [@user] [label:метка]")
    hack = await ctx.hackathon(message.chat.id, cmd.tokens[0].text)
    require_repo(hack)
    label = None
    user_tokens = []
    for token in cmd.tokens[1:]:
        if token.text.lower().startswith("label:"):
            label = token.text[len("label:") :].strip() or None
            if label is None:
                raise UserError('Укажите метку: label:bug или label:"in progress"')
        else:
            user_tokens.append(token)
    assignee_login = None
    user_id = await _target_user(message, ctx, is_admin, user_tokens)
    if user_id is not None:
        assignee_login = await ctx.storage.github_links.get_login(user_id)
        if not assignee_login:
            raise UserError("У этого человека не привязан GitHub-логин (/link_github)")
    query = IssueQuery(message.chat.id, hack.id, assignee_login, label)
    token = ctx.remember_issue_query(query)
    text, keyboard = await _render_issues_page(ctx, query, 0, token)
    await message.reply(text, reply_markup=keyboard, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("iss:"))
async def cb_issues_page(callback: CallbackQuery, ctx: AppContext) -> None:
    assert callback.data is not None
    _, token, raw_page = callback.data.split(":", 2)
    query = ctx.issue_queries.get(token)
    message = callback.message
    if query is None or message is None or query.chat_id != message.chat.id:
        await callback.answer("Список устарел, повторите /issues", show_alert=True)
        return
    text, keyboard = await _render_issues_page(ctx, query, int(raw_page), token)
    await callback.answer()
    await message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=keyboard, link_preview_options=NO_PREVIEW
    )


@router.message(Command("issue_new"))
async def cmd_issue_new(message: Message, ctx: AppContext, is_admin: bool) -> None:
    cmd = parse_command(message)
    if len(cmd.tokens) < 2:
        raise usage("/issue_new <хак> [@исполнитель] <название>\n<описание с новой строки>")
    assert message.from_user is not None
    caller = message.from_user.id
    hack = await ctx.hackathon(message.chat.id, cmd.tokens[0].text)
    if not is_admin and not await ctx.storage.hackathons.is_member(hack.id, caller):
        raise UserError(
            f"Создавать issues в {escape(hack.slug)} могут участники хакатона и администраторы бота"
        )
    title_index = 1
    assignee_id = None
    second = cmd.tokens[1]
    if second.is_explicit_user or (is_admin and second.ref_kind == "id" and len(cmd.tokens) > 2):
        assignee_id = await resolve_user_token(
            second,
            chat_id=message.chat.id,
            caller_id=caller,
            is_admin=is_admin,
            directory=ctx.directory,
        )
        title_index = 2
    if assignee_id is not None and assignee_id != caller and not is_admin:
        raise UserError("Назначать issue на других могут только администраторы бота")
    rest = cmd.rest(title_index)
    title, _, body = rest.partition("\n")
    title = title.strip()
    if not title:
        raise UserError("Укажите название issue")
    if len(title) > 256:
        raise UserError("Название слишком длинное (максимум 256 символов)")
    body = body.strip()
    footer = "_Создано через Telegram-бота команды_"
    body = f"{body}\n\n{footer}" if body else footer
    result = await ctx.tasks.create_issue(hack, title, body, assignee_id)
    issue = result.issue
    lines = [f"✅ Создан {issue_link(issue.number, issue.title, issue.html_url)}"]
    if assignee_id is not None:
        name = await ctx.directory.plain(message.chat.id, assignee_id)
        lines.append(f"Задача назначена: {name}")
    lines.extend(f"⚠️ {w}" for w in result.warnings)
    await reply(message, "\n".join(lines))


@router.message(Command("link_github"))
async def cmd_link_github(message: Message, ctx: AppContext, is_admin: bool) -> None:
    cmd = parse_command(message)
    assert message.from_user is not None
    caller = message.from_user.id
    if len(cmd.tokens) == 1:
        target = caller
        if is_admin and (reply_user := reply_target(message)) is not None:
            await ctx.directory.remember(reply_user)
            target = reply_user.id
        raw_login = cmd.tokens[0].text
    elif len(cmd.tokens) == 2:
        target = await resolve_user_token(
            cmd.tokens[0],
            chat_id=message.chat.id,
            caller_id=caller,
            is_admin=is_admin,
            directory=ctx.directory,
        )
        raw_login = cmd.tokens[1].text
    else:
        raise usage("/link_github <логин>   🔒 /link_github @user <логин>")
    if target != caller and not is_admin:
        raise UserError("Привязать GitHub за другого человека может только администратор бота")
    login = normalize_login(raw_login)
    owner = await ctx.storage.github_links.get_user_by_login(login)
    if owner is not None and owner != target:
        name = await ctx.directory.plain(message.chat.id, owner)
        raise UserError(f"GitHub-логин {escape(login)} уже привязан к {name}")
    if ctx.github.configured and not await ctx.github.user_exists(login):
        raise UserError(f"Пользователь GitHub «{escape(login)}» не найден")
    await ctx.storage.github_links.set_login(target, login)
    name = await ctx.directory.plain(message.chat.id, target)
    await message.reply(f"✅ GitHub {escape(login)} привязан к {name}")


@router.message(Command("unlink_github"))
async def cmd_unlink_github(message: Message, ctx: AppContext, is_admin: bool) -> None:
    cmd = parse_command(message)
    assert message.from_user is not None
    caller = message.from_user.id
    target = caller
    if cmd.tokens:
        target = await resolve_user_token(
            cmd.tokens[0],
            chat_id=message.chat.id,
            caller_id=caller,
            is_admin=is_admin,
            directory=ctx.directory,
        )
    elif is_admin and (reply_user := reply_target(message)) is not None:
        target = reply_user.id
    if target != caller and not is_admin:
        raise UserError("Отвязать GitHub другого человека может только администратор бота")
    removed = await ctx.storage.github_links.remove(target)
    name = await ctx.directory.plain(message.chat.id, target)
    await message.reply(
        f"GitHub отвязан от {name}" if removed else f"У {name} GitHub не был привязан"
    )
