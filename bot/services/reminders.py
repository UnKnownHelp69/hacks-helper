"""Deadline reminders with deduplication and availability-aware deferral."""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.db.database import utcnow
from bot.db.models import MILESTONES, Hackathon
from bot.db.repositories import Storage
from bot.services.notify import Messenger, Pinger
from bot.services.schedule_logic import MILESTONE_UNTIL, due_reminder_offsets
from bot.services.timeutil import format_dt, format_duration, tz_label

log = logging.getLogger(__name__)


async def chat_zone(storage: Storage, chat_id: int, default: str) -> ZoneInfo:
    name = await storage.chats.get_timezone(chat_id)
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("Invalid timezone %r stored for chat %s, using default", name, chat_id)
        return ZoneInfo(default)


def reminder_header(hack: Hackathon, milestone: str, now: datetime, tz: ZoneInfo) -> str:
    at = hack.milestone_at(milestone)
    return (
        f"⏰ <b>{escape(hack.slug)}</b>: {MILESTONE_UNTIL[milestone]} осталось "
        f"{format_duration(at - now)} ({format_dt(at, tz)} {tz_label(tz)})"
    )


class ReminderService:
    def __init__(
        self, settings: Settings, storage: Storage, pinger: Pinger, messenger: Messenger
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.pinger = pinger
        self.messenger = messenger

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        await self.send_due_reminders(now)
        await self.process_deferred(now)

    async def send_due_reminders(self, now: datetime) -> None:
        for hack in await self.storage.hackathons.list_all_active():
            if not self.settings.is_chat_allowed(hack.chat_id):
                continue
            for milestone in MILESTONES:
                try:
                    await self._remind(hack, milestone, now)
                except Exception:
                    log.exception("Reminder failed for hackathon %s/%s", hack.id, milestone)

    async def _remind(self, hack: Hackathon, milestone: str, now: datetime) -> None:
        at = hack.milestone_at(milestone)
        sent = await self.storage.reminders.sent_offsets(hack.id, milestone, at)
        due = due_reminder_offsets(at, self.settings.reminder_offsets_minutes, now, sent)
        if not due:
            return
        # Mark first: a crash after sending must not cause a duplicate after restart.
        await self.storage.reminders.mark_sent(hack.id, milestone, at, due)
        tz = await chat_zone(self.storage, hack.chat_id, self.settings.default_timezone)
        members = await self.storage.hackathons.members(hack.id)
        plan = await self.pinger.plan(hack.chat_id, members, tz=tz, now=now, deadline=at)
        for user_id, when in plan.deferred.items():
            await self.storage.reminders.add_deferred(hack.id, milestone, at, user_id, when)
        lines = [reminder_header(hack, milestone, now, tz), *plan.render_lines()]
        if not members:
            lines.append("Участники ещё не добавлены")
        log.info("Reminder %s/%s (offsets %s) sent", hack.slug, milestone, due)
        await self.messenger.send(hack.chat_id, "\n".join(lines))

    async def process_deferred(self, now: datetime) -> None:
        pending = await self.storage.reminders.list_deferred()
        groups: dict[tuple[int, str], list[int]] = {}
        hacks: dict[int, Hackathon | None] = {}
        for ping in pending:
            if ping.hackathon_id not in hacks:
                hacks[ping.hackathon_id] = await self.storage.hackathons.get_by_id(
                    ping.hackathon_id
                )
            hack = hacks[ping.hackathon_id]
            stale = (
                hack is None
                or not hack.is_active
                or not self.settings.is_chat_allowed(hack.chat_id)
                or hack.milestone_at(ping.milestone) != ping.milestone_at
                or now >= ping.milestone_at
                or not await self.storage.hackathons.is_member(hack.id, ping.user_id)
            )
            if stale:
                await self.storage.reminders.delete_deferred(
                    ping.hackathon_id, ping.milestone, ping.user_id
                )
                continue
            assert hack is not None
            tz = await chat_zone(self.storage, hack.chat_id, self.settings.default_timezone)
            if await self.pinger.user_available(ping.user_id, tz, now):
                groups.setdefault((hack.id, ping.milestone), []).append(ping.user_id)

        for (hack_id, milestone), user_ids in groups.items():
            hack = hacks[hack_id]
            assert hack is not None
            for user_id in user_ids:
                await self.storage.reminders.delete_deferred(hack_id, milestone, user_id)
            tz = await chat_zone(self.storage, hack.chat_id, self.settings.default_timezone)
            plan = await self.pinger.plan(hack.chat_id, user_ids, tz=tz, now=now)
            if not plan.mentions and not plan.not_in_group:
                continue
            lines = [reminder_header(hack, milestone, now, tz) + " — отложенное напоминание"]
            if plan.mentions:
                lines.append("👥 " + ", ".join(plan.mentions))
            lines.extend(f"🚪 {name} нет в группе" for name in plan.not_in_group)
            await self.messenger.send(hack.chat_id, "\n".join(lines))
