"""General commands: /help and /me."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.context import AppContext
from bot.handlers.views import me_view
from bot.respond import reply, respond
from bot.texts import HELP_TEXT

router = Router(name="common")


@router.message(Command("help"))
async def cmd_help(message: Message, ctx: AppContext) -> None:
    if ctx.settings.setup_mode:
        await reply(
            message,
            f"chat_id: <code>{message.chat.id}</code>\n"
            "Добавьте в ALLOWED_CHAT_IDS и перезапустите бота.",
        )
        return
    await reply(message, HELP_TEXT)


@router.message(Command("me"))
async def cmd_me(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    if not ctx.settings.is_chat_allowed(message.chat.id):
        # Before setup, reveal only the caller's own id, without reusable buttons.
        await reply(message, f"Ваш user_id: <code>{message.from_user.id}</code>")
        return
    text = await me_view(ctx, message.chat.id, message.from_user)
    await respond(message, text, ctx, personal="me")
