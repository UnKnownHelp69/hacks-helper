"""Shared owner-bound buttons and callback validation for personal menus."""

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.context import AppContext
from bot.db.models import HackSession
from bot.errors import UserError


def session_button(
    namespace: str,
    session: HackSession,
    text: str,
    action: str,
    argument: str | int = "",
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=f"{namespace}:{session.token}:{session.revision}:{action}:{argument}",
    )


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def callback_session(
    callback: CallbackQuery, ctx: AppContext, command: str
) -> tuple[HackSession, str, str]:
    parts = (callback.data or "").split(":", 4)
    if len(parts) != 5 or not parts[2].isdigit():
        raise UserError(f"Кнопка устарела. Откройте {command}")
    _, token, revision, action, argument = parts
    session = await ctx.hacks.session(callback.from_user.id, token, command=command)
    if callback.message is None or callback.message.chat.id != session.chat_id:
        raise UserError("Эта кнопка относится к другому чату")
    if session.revision != int(revision):
        raise UserError(f"Кнопка устарела. Откройте {command} или продолжите последний диалог")
    return session, action, argument
