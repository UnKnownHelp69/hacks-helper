"""Helpers for building HTML messages within Telegram limits."""

from __future__ import annotations

import re
from html import escape, unescape

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3900


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split text into chunks on line boundaries.

    Count visible UTF-16 units, preserving complete HTML links and entities.
    An overlong single line is shortened as escaped plain text.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        plain = unescape(re.sub(r"<[^>]+>", "", line))
        units = len(plain.encode("utf-16-le")) // 2
        if units > limit:
            clipped = plain.encode("utf-16-le")[: (limit - 1) * 2].decode(
                "utf-16-le", errors="ignore"
            )
            line = escape(clipped) + "…"
            units = len((clipped + "…").encode("utf-16-le")) // 2
        extra = units + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
            extra = units
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()] or [""]


def link(url: str, title: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(title)}</a>'


def issue_link(number: int, title: str, url: str) -> str:
    label = f"#{number} {title}".strip()
    if len(label) > 120:
        label = label[:119] + "…"
    return link(url, label) if url else escape(label)
