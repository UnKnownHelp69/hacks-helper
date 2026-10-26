"""Shared helpers for handlers."""

from __future__ import annotations

import re
from html import escape

from bot.errors import UserError
from bot.respond import NO_PREVIEW, reply  # noqa: F401  (re-exported for handlers)

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
RESERVED_SLUGS = {"me", "all", "help"}
_ISSUE_RE = re.compile(r"^#?(\d{1,9})$")


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
        raise UserError(f"Неверный номер issue: {escape(raw)}")
    return int(match.group(1))


def usage(text: str) -> UserError:
    return UserError(f"Формат: {escape(text)}")
