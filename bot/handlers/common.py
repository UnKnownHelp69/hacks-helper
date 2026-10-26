"""General commands: /help, /me, /whoami, /chatid, /tz."""

from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.context import AppContext
from bot.db.database import utcnow
from bot.handlers.base import reply
from bot.services.timeutil import format_dt, utc_offset_label
from bot.texts import HELP_TEXT, format_offsets

log = logging.getLogger(__name__)

router = Router(name="common")
GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})


@router.message(Command("whoami"), F.chat.type == ChatType.PRIVATE)
async def cmd_whoami(message: Message) -> None:
    # Only the caller's own id; nothing else is revealed in private chats.
    assert message.from_user is not None
    await message.answer(f"Ваш Telegram user_id: <code>{message.from_user.id}</code>")


@router.message(Command("chatid"), GROUPS)
async def cmd_chatid(message: Message, ctx: AppContext) -> None:
    chat_id = message.chat.id
    if ctx.settings.setup_mode:
        log.warning(
            "Setup mode: /chatid in chat %s ('%s'). Put it into ALLOWED_CHAT_IDS and restart.",
            chat_id,
            message.chat.title,
        )
        await message.reply(
            f"id этого чата: <code>{chat_id}</code>\n"
            "Бот в режиме первичной настройки: добавьте этот id в ALLOWED_CHAT_IDS в .env "
            "и перезапустите бота."
        )
        return
    log.info("/chatid in chat %s", chat_id)
    await message.reply(f"id этого чата: <code>{chat_id}</code>")


@router.message(Command("help", "start"), GROUPS)
async def cmd_help(message: Message, ctx: AppContext) -> None:
    text = HELP_TEXT.replace("{label}", escape(ctx.settings.github_in_progress_label)).replace(
        "{offsets}", format_offsets(ctx.settings.reminder_offsets_minutes)
    )
    await reply(message, text)


@router.message(Command("me"), GROUPS)
async def cmd_me(message: Message, ctx: AppContext) -> None:
    user = message.from_user
    assert user is not None
    # The registry middleware has already stored the user; make sure it is fresh.
    await ctx.directory.remember(user)
    ctx.directory.invalidate(message.chat.id, user.id)
    login = await ctx.storage.github_links.get_login(user.id)
    username = f"@{escape(user.username)}" if user.username else "нет (можно упоминать по имени)"
    await message.reply(
        "✅ Вы зарегистрированы.\n"
        f"user_id: <code>{user.id}</code>\n"
        f"username: {username}\n"
        f"GitHub: {escape(login) if login else 'не привязан (/link_github &lt;логин&gt;)'}"
    )


@router.message(Command("tz"), GROUPS)
async def cmd_tz(message: Message, ctx: AppContext) -> None:
    tz = await ctx.zone(message.chat.id)
    now = utcnow()
    await message.reply(
        f"Часовой пояс чата: <b>{escape(tz.key)}</b> ({utc_offset_label(tz, now)})\n"
        f"Сейчас: {format_dt(now, tz)}"
    )
