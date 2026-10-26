"""Per-user weekly availability ("when it is OK to ping me").

Every weekday has one of three modes:
  * ``all``     - can be pinged the whole day (default);
  * ``none``    - must not be pinged that day;
  * ``windows`` - only inside the listed time windows.

A window belongs to the day it starts on. A window that crosses midnight
(e.g. Friday 22:00-02:00) therefore also covers Saturday 00:00-02:00, even if
Saturday itself is ``none``. All times are interpreted in the chat time zone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from html import escape

from bot.errors import UserError

MODE_ALL = "all"
MODE_NONE = "none"
MODE_WINDOWS = "windows"

WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

_DAY_ALIASES: dict[str, int] = {}
for _idx, _names in enumerate(
    [
        ("пн", "пон", "понедельник", "mon", "monday"),
        ("вт", "вто", "вторник", "tue", "tuesday"),
        ("ср", "сре", "среда", "wed", "wednesday"),
        ("чт", "чет", "четверг", "thu", "thursday"),
        ("пт", "пят", "пятница", "fri", "friday"),
        ("сб", "суб", "суббота", "sat", "saturday"),
        ("вс", "вос", "воскресенье", "sun", "sunday"),
    ]
):
    for _name in _names:
        _DAY_ALIASES[_name] = _idx

_DAY_GROUPS: dict[str, list[int]] = {
    "все": list(range(7)),
    "всё": list(range(7)),
    "ежедневно": list(range(7)),
    "all": list(range(7)),
    "будни": [0, 1, 2, 3, 4],
    "weekdays": [0, 1, 2, 3, 4],
    "выходные": [5, 6],
    "weekend": [5, 6],
}

_YES = {"да", "можно", "всегда", "весь", "+", "yes", "all"}
_NO = {"нет", "нельзя", "никогда", "-", "no", "none"}

_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")


@dataclass(frozen=True)
class Window:
    """A time window in minutes from midnight. end <= start means it crosses midnight."""

    start: int
    end: int

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    def __str__(self) -> str:
        return f"{_fmt_minutes(self.start)}–{_fmt_minutes(self.end)}"


@dataclass(frozen=True)
class DaySchedule:
    mode: str = MODE_ALL
    windows: tuple[Window, ...] = ()

    def describe(self) -> str:
        if self.mode == MODE_ALL:
            return "весь день"
        if self.mode == MODE_NONE:
            return "нельзя"
        return ", ".join(str(w) for w in self.windows)


@dataclass
class WeeklySchedule:
    days: dict[int, DaySchedule] = field(default_factory=dict)

    def day(self, weekday: int) -> DaySchedule:
        return self.days.get(weekday, DaySchedule())

    @property
    def is_default(self) -> bool:
        return all(self.day(d).mode == MODE_ALL for d in range(7))


def _fmt_minutes(value: int) -> str:
    hours, minutes = divmod(value, 60)
    return f"{hours:02d}:{minutes:02d}"


def _parse_time(raw: str, *, allow_24: bool) -> int:
    match = _TIME_RE.match(raw.strip())
    if not match:
        raise UserError(f"Неверное время «{escape(raw)}». Формат ЧЧ:ММ, например 09:00")
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    if minutes >= 60 or hours > 24 or (hours == 24 and (minutes or not allow_24)):
        raise UserError(f"Неверное время «{escape(raw)}»")
    return hours * 60 + minutes


def parse_windows(raw: str) -> tuple[Window, ...]:
    """Parse '09:00-21:00,22:00-02:00' into windows."""
    windows = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        pieces = re.split(r"[-–—]", part)
        if len(pieces) != 2:
            raise UserError(
                f"Неверное окно «{escape(part)}». "
                "Формат: 09:00-21:00 (можно несколько через запятую)"
            )
        start = _parse_time(pieces[0], allow_24=False)
        end = _parse_time(pieces[1], allow_24=True) % (24 * 60)
        if start == end:
            raise UserError(
                f"Окно «{escape(part)}» имеет нулевую длину. Для целого дня используйте «да»"
            )
        windows.append(Window(start, end))
    if not windows:
        raise UserError("Не указаны окна времени")
    return tuple(windows)


def parse_day_value(raw: str) -> DaySchedule:
    value = raw.strip().lower()
    if value in _YES:
        return DaySchedule(MODE_ALL)
    if value in _NO:
        return DaySchedule(MODE_NONE)
    return DaySchedule(MODE_WINDOWS, parse_windows(raw))


def parse_days(raw: str) -> list[int]:
    """Parse 'пн', 'пн-пт', 'сб,вс', 'будни', 'все' into weekday indexes (0 = Monday)."""
    result: list[int] = []
    for part in raw.lower().split(","):
        part = part.strip()
        if not part:
            continue
        if part in _DAY_GROUPS:
            result.extend(_DAY_GROUPS[part])
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            if first not in _DAY_ALIASES or last not in _DAY_ALIASES:
                raise UserError(f"Неизвестный диапазон дней «{escape(part)}». Пример: пн-пт")
            start, end = _DAY_ALIASES[first], _DAY_ALIASES[last]
            day = start
            while True:
                result.append(day)
                if day == end:
                    break
                day = (day + 1) % 7
            continue
        if part not in _DAY_ALIASES:
            raise UserError(
                f"Неизвестный день «{escape(part)}». Используйте пн, вт, ср, чт, пт, сб, вс, "
                "будни, выходные или все"
            )
        result.append(_DAY_ALIASES[part])
    if not result:
        raise UserError("Не указаны дни недели")
    return sorted(set(result))


def serialize_day(day: DaySchedule) -> tuple[str, str]:
    windows = ",".join(f"{_fmt_minutes(w.start)}-{_fmt_minutes(w.end)}" for w in day.windows)
    return day.mode, windows


def deserialize_schedule(raw: dict[int, tuple[str, str]]) -> WeeklySchedule:
    days: dict[int, DaySchedule] = {}
    for weekday, (mode, windows) in raw.items():
        if mode == MODE_WINDOWS:
            days[weekday] = DaySchedule(MODE_WINDOWS, parse_windows(windows))
        elif mode in (MODE_ALL, MODE_NONE):
            days[weekday] = DaySchedule(mode)
    return WeeklySchedule(days)


def is_available(schedule: WeeklySchedule, local_now: datetime) -> bool:
    """Whether the user may be pinged at ``local_now`` (aware datetime in the chat time zone)."""
    weekday = local_now.weekday()
    minute = local_now.hour * 60 + local_now.minute
    today = schedule.day(weekday)
    if today.mode == MODE_ALL:
        return True
    if today.mode == MODE_WINDOWS:
        for w in today.windows:
            if w.crosses_midnight:
                if minute >= w.start:
                    return True
            elif w.start <= minute < w.end:
                return True
    # Overnight windows that started yesterday.
    yesterday = schedule.day((weekday - 1) % 7)
    if yesterday.mode == MODE_WINDOWS:
        for w in yesterday.windows:
            if w.crosses_midnight and minute < w.end:
                return True
    return False


def next_available(schedule: WeeklySchedule, local_now: datetime) -> datetime | None:
    """Return the nearest moment >= local_now when pinging is allowed, or None if never."""
    if is_available(schedule, local_now):
        return local_now
    tz = local_now.tzinfo
    candidates: list[datetime] = []
    today = local_now.date()
    for offset in range(8):
        day = today + timedelta(days=offset)
        sched = schedule.day(day.weekday())
        starts: list[int] = []
        if sched.mode == MODE_ALL:
            starts.append(0)
        elif sched.mode == MODE_WINDOWS:
            starts.extend(w.start for w in sched.windows)
        for start in starts:
            moment = datetime.combine(day, time(start // 60, start % 60), tzinfo=tz)
            if moment > local_now:
                candidates.append(moment)
        if candidates:
            break
    return min(candidates) if candidates else None


def describe_schedule(schedule: WeeklySchedule) -> str:
    return "\n".join(f"{WEEKDAY_NAMES[d]}: {schedule.day(d).describe()}" for d in range(7))
