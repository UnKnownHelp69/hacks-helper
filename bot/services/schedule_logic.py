"""Pure logic around hackathon dates: validation, milestones, /left, reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.db.models import (
    MILESTONE_END,
    MILESTONE_REGISTRATION,
    MILESTONE_START,
    MILESTONES,
    Hackathon,
)
from bot.errors import UserError

MILESTONE_TITLES = {
    MILESTONE_REGISTRATION: "окончание регистрации",
    MILESTONE_START: "начало хакатона",
    MILESTONE_END: "окончание хакатона",
}

MILESTONE_UNTIL = {
    MILESTONE_REGISTRATION: "до окончания регистрации",
    MILESTONE_START: "до начала хакатона",
    MILESTONE_END: "до окончания хакатона",
}

MILESTONE_ALIASES = {
    "reg": MILESTONE_REGISTRATION,
    "registration": MILESTONE_REGISTRATION,
    "рег": MILESTONE_REGISTRATION,
    "регистрация": MILESTONE_REGISTRATION,
    "start": MILESTONE_START,
    "начало": MILESTONE_START,
    "старт": MILESTONE_START,
    "end": MILESTONE_END,
    "конец": MILESTONE_END,
    "финиш": MILESTONE_END,
}


def parse_milestone(raw: str) -> str:
    try:
        return MILESTONE_ALIASES[raw.lower()]
    except KeyError:
        raise UserError(
            "Укажите, какую дату менять: reg (регистрация), start (начало) или end (конец)"
        ) from None


def validate_dates(registration_end: datetime, start_at: datetime, end_at: datetime) -> None:
    if registration_end > start_at:
        raise UserError("Окончание регистрации должно быть не позже начала хакатона")
    if start_at >= end_at:
        raise UserError("Начало хакатона должно быть раньше его окончания")


@dataclass(frozen=True)
class NextMilestone:
    milestone: str
    at: datetime
    remaining: timedelta


def next_milestone(hack: Hackathon, now: datetime) -> NextMilestone | None:
    """Nearest milestone strictly in the future, or None if the hackathon is over."""
    for milestone in MILESTONES:
        at = hack.milestone_at(milestone)
        if at > now:
            return NextMilestone(milestone, at, at - now)
    return None


def current_phase(hack: Hackathon, now: datetime) -> str:
    if now < hack.registration_end:
        return "идёт регистрация"
    if now < hack.start_at:
        return "регистрация закрыта, ждём старта"
    if now < hack.end_at:
        return "хакатон идёт"
    return "хакатон завершён"


def due_reminder_offsets(
    milestone_at: datetime, offsets_minutes: list[int], now: datetime, already_sent: set[int]
) -> list[int]:
    """Offsets whose reminder time has come for a future milestone and weren't sent yet.

    Several offsets may be due at once (e.g. after downtime or when a date was
    moved closer); the caller sends a single reminder and marks all of them.
    """
    if now >= milestone_at:
        return []
    return [
        offset
        for offset in offsets_minutes
        if offset not in already_sent and milestone_at - timedelta(minutes=offset) <= now
    ]


def should_archive(hack: Hackathon, now: datetime) -> bool:
    return hack.is_active and now >= hack.end_at
