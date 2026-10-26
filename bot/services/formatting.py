"""Helpers for building HTML messages within Telegram limits."""

from __future__ import annotations

from html import escape

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3900


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split text into chunks on line boundaries.

    Each line is expected to be self-contained HTML, so splitting between lines
    never breaks markup. Overlong single lines are hard-cut (rare: issue titles
    are limited to 256 chars by GitHub).
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
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
