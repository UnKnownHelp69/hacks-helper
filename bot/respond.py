"""Answering commands: short answers go to a popup, long ones to the chat.

Telegram has no per-user messages in groups. A short answer (<= 200 characters,
the popup limit) is therefore posted as a small message with a button; whoever
presses it sees the answer in a popup visible only to them. Longer answers are
posted to the chat as usual.

"Personal" buttons (my tasks, my profile, my schedule) are computed for the
person who presses them, so one button serves everyone.
"""

from __future__ import annotations

import re
from html import escape, unescape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from bot.context import AppContext
from bot.services.formatting import split_message

POPUP_LIMIT = 200  # Telegram limit for callback query answers

PERSONAL_LABELS = {
    "me": "👀 Мой профиль",
    "tasks": "👀 Мои задачи",
    "avail": "👀 Моё расписание",
}

_TAG_RE = re.compile(r"<[^>]+>")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def reply(message: Message, text: str) -> None:
    """Reply with HTML in the chat, split to respect Telegram limits."""
    for index, chunk in enumerate(split_message(text)):
        if index == 0:
            await message.reply(chunk, link_preview_options=NO_PREVIEW)
        else:
            await message.answer(chunk, link_preview_options=NO_PREVIEW)


def html_to_plain(text: str) -> str:
    return unescape(_TAG_RE.sub("", text)).strip()


def fits_popup(text: str) -> bool:
    plain = html_to_plain(text)
    return 0 < len(plain.encode("utf-16-le")) // 2 <= POPUP_LIMIT


def _command_echo(message: Message) -> str:
    line = (message.text or message.caption or "").split("\n", 1)[0].strip()
    return line[:60] + ("…" if len(line) > 60 else "")


def _button(label: str, data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=data)]]
    )


async def respond(
    message: Message, text: str, ctx: AppContext, *, personal: str | None = None
) -> None:
    """Answer a command: popup behind a button if it fits, otherwise a chat message."""
    if not fits_popup(text):
        await reply(message, text)
        return
    if personal is not None:
        markup = _button(PERSONAL_LABELS[personal], f"my:{personal}")
    else:
        popup_id = await ctx.storage.popups.add(message.chat.id, html_to_plain(text))
        markup = _button("👀 Показать ответ", f"pop:{popup_id}")
    await message.reply(f"👀 {escape(_command_echo(message))}", reply_markup=markup)
