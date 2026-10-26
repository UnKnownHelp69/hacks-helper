"""Parsing and formatting of dates, durations and time zones."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.errors import UserError

DATE_FORMAT_HINT = "ДД.ММ.ГГГГ ЧЧ:ММ"
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def get_zone(name: str) -> ZoneInfo:
    """Return a ZoneInfo for an IANA name or raise a user-facing error."""
    name = name.strip()
    if not name or ".." in name or name.startswith("/"):
        raise UserError(f"Неизвестный часовой пояс «{escape(name)}». Пример: Europe/Moscow")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UserError(
            f"Неизвестный часовой пояс «{escape(name)}». Используйте IANA-название, "
            "например Europe/Moscow или Asia/Yekaterinburg"
        ) from exc


def parse_datetime(date_part: str, time_part: str, tz: ZoneInfo) -> datetime:
    """Parse 'ДД.ММ.ГГГГ' + 'ЧЧ:ММ' in the chat time zone and return an aware UTC datetime."""
    raw = escape(f"{date_part} {time_part}".strip())
    date_match = _DATE_RE.match(date_part.strip())
    time_match = _TIME_RE.match(time_part.strip())
    if not date_match or not time_match:
        raise UserError(
            f"Неверная дата «{raw}». Формат: {DATE_FORMAT_HINT}, например 01.11.2026 18:00"
        )
    day, month, year = (int(g) for g in date_match.groups())
    hour, minute = (int(g) for g in time_match.groups())
    try:
        local = datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError as exc:
        raise UserError(f"Такой даты не существует: «{raw}»") from exc
    return local.astimezone(UTC)


def parse_datetime_tokens(tokens: list[str], tz: ZoneInfo) -> datetime:
    """Parse a datetime from exactly two tokens (date and time)."""
    if len(tokens) != 2:
        raise UserError(f"Ожидалась дата в формате {DATE_FORMAT_HINT}")
    return parse_datetime(tokens[0], tokens[1], tz)


def format_dt(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def tz_label(tz: ZoneInfo) -> str:
    return "МСК" if tz.key == "Europe/Moscow" else tz.key


def format_duration(delta: timedelta) -> str:
    """Format a positive timedelta as 'N д N ч N мин' (rounded down to minutes)."""
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes or not parts:
        parts.append(f"{minutes} мин")
    return " ".join(parts)


def utc_offset_label(tz: ZoneInfo, at: datetime) -> str:
    offset = at.astimezone(tz).utcoffset() or timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
