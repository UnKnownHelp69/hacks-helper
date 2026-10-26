"""Shared helpers for handlers."""

from __future__ import annotations

import re
from html import escape

from aiogram.types import LinkPreviewOptions, Message

from bot.errors import UserError
from bot.services.formatting import split_message

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
RESERVED_SLUGS = {"me", "all", "help"}
_ISSUE_RE = re.compile(r"^#?(\d{1,9})$")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def reply(message: Message, text: str) -> None:
    """Reply with HTML, splitting long texts to respect Telegram limits."""
    chunks = split_message(text)
    for index, chunk in enumerate(chunks):
        if index == 0:
            await message.reply(chunk, link_preview_options=NO_PREVIEW)
        else:
            await message.answer(chunk, link_preview_options=NO_PREVIEW)


def validate_slug(raw: str) -> str:
    slug = raw.strip().lower()
    if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
        raise UserError(
            f"Неверное имя хакатона «{escape(raw)}». Нужно 2–32 символа: латинские буквы, "
            "цифры, «-» и «_», начинается с буквы. Например: hackaton2026"
        )
    return slug


def parse_issue_number(raw: str) -> int:
    match = _ISSUE_RE.match(raw.strip())
    if not match:
        raise UserError(f"Неверный номер issue «{escape(raw)}». Пример: 12 или #12")
    return int(match.group(1))


def usage(text: str) -> UserError:
    return UserError(f"Использование: <code>{escape(text)}</code>\nПодробнее: /help")
