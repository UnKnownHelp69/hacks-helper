"""Deadline reminders with deduplication and availability-aware deferral."""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.db.database import utcnow
from bot.db.models import MILESTONE_END, MILESTONES, Hackathon
from bot.db.repositories import Storage
from bot.services.notify import Messenger, Pinger
from bot.services.schedule_logic import MILESTONE_TITLES, MILESTONE_UNTIL, due_reminder_offsets
from bot.services.timeutil import format_dt, format_duration, tz_label

log = logging.getLogger(__name__)


async def chat_zone(storage: Storage, chat_id: int, default: str) -> ZoneInfo:
    name = await storage.chats.get_timezone(chat_id)
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("Invalid timezone %r stored for chat %s, using default", name, chat_id)
        return ZoneInfo(default)


def reminder_header(
    hack: Hackathon,
    milestone: str,
    now: datetime,
    tz: ZoneInfo,
    at: datetime | None = None,
    title: str | None = None,
) -> str:
    at = at or hack.milestone_at(milestone)
    until = MILESTONE_UNTIL[milestone] if title is None else f"до «{escape(title)}»"
    return (
        f"⏰ <b>{escape(hack.slug)}</b>: {until} осталось "
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
        await self.send_milestone_announcements(now)
        await self.send_due_reminders(now)
        await self.process_deferred(now)

    async def milestones(self, hack: Hackathon) -> list[tuple[str, datetime, str | None]]:
        result = [(key, hack.milestone_at(key), None) for key in MILESTONES]
        result.extend(
            (f"checkpoint_{checkpoint.id}", checkpoint.at, checkpoint.title)
            for checkpoint in await self.storage.workflow.checkpoints(hack.id)
        )
        return result

    async def send_milestone_announcements(self, now: datetime) -> None:
        for hack in await self.storage.hackathons.list_all_active():
            if not self.settings.is_chat_allowed(hack.chat_id):
                continue
            for key, at, title in await self.milestones(hack):
                # ArchiveService announces the finish, preserving its final GitHub sync.
                if key == MILESTONE_END or at > now or at < hack.created_at:
                    continue
                sent = await self.storage.reminders.sent_offsets(hack.id, key, at)
                if 0 in sent:
                    continue
                tz = await chat_zone(self.storage, hack.chat_id, self.settings.default_timezone)
                event_title = title or MILESTONE_TITLES[key]
                text = (
                    f"📍 <b>{escape(hack.display_name)}</b>: {escape(event_title)}\n"
                    f"{format_dt(at, tz)} {tz_label(tz)}"
                )
                if await self.messenger.send(hack.chat_id, text):
                    await self.storage.reminders.mark_sent(hack.id, key, at, [0])

    async def send_due_reminders(self, now: datetime) -> None:
        for hack in await self.storage.hackathons.list_all_active():
            if not self.settings.is_chat_allowed(hack.chat_id):
                continue
            for milestone, at, title in await self.milestones(hack):
                try:
                    await self._remind(hack, milestone, now, at, title)
                except Exception:
                    log.exception("Reminder failed for hackathon %s/%s", hack.id, milestone)

    async def _remind(
        self,
        hack: Hackathon,
        milestone: str,
        now: datetime,
        at: datetime | None = None,
        title: str | None = None,
    ) -> None:
        at = at or hack.milestone_at(milestone)
        sent = await self.storage.reminders.sent_offsets(hack.id, milestone, at)
        due = due_reminder_offsets(at, self.settings.reminder_offsets_minutes, now, sent)
        if not due:
            return
        tz = await chat_zone(self.storage, hack.chat_id, self.settings.default_timezone)
        members = await self.storage.hackathons.members(hack.id)
        plan = await self.pinger.plan(hack.chat_id, members, tz=tz, now=now, deadline=at)
        for user_id, when in plan.deferred.items():
            await self.storage.reminders.add_deferred(hack.id, milestone, at, user_id, when)
        lines = [reminder_header(hack, milestone, now, tz, at, title), *plan.render_lines()]
        if not members:
            lines.append("Участников нет")
        if await self.messenger.send(hack.chat_id, "\n".join(lines)):
            await self.storage.reminders.mark_sent(hack.id, milestone, at, due)
            log.info("Reminder %s/%s (offsets %s) sent", hack.slug, milestone, due)

    async def process_deferred(self, now: datetime) -> None:
        pending = await self.storage.reminders.list_deferred()
        groups: dict[tuple[int, str], list[int]] = {}
        hacks: dict[int, Hackathon | None] = {}
        milestones: dict[int, dict[str, tuple[datetime, str | None]]] = {}
        for ping in pending:
            if ping.hackathon_id not in hacks:
                hacks[ping.hackathon_id] = await self.storage.hackathons.get_by_id(
                    ping.hackathon_id
                )
            hack = hacks[ping.hackathon_id]
            if hack and hack.id not in milestones:
                milestones[hack.id] = {
                    key: (at, title) for key, at, title in await self.milestones(hack)
                }
            current = milestones.get(ping.hackathon_id, {}).get(ping.milestone)
            stale = (
                hack is None
                or not hack.is_active
                or not self.settings.is_chat_allowed(hack.chat_id)
                or current is None
                or current[0] != ping.milestone_at
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
            tz = await chat_zone(self.storage, hack.chat_id, self.settings.default_timezone)
            plan = await self.pinger.plan(hack.chat_id, user_ids, tz=tz, now=now)
            if not plan.mentions and not plan.not_in_group:
                continue
            at, title = milestones[hack_id][milestone]
            lines = [reminder_header(hack, milestone, now, tz, at, title)]
            if plan.mentions:
                lines.append("👥 " + ", ".join(plan.mentions))
            lines.extend(f"🚪 {name} нет в группе" for name in plan.not_in_group)
            if await self.messenger.send(hack.chat_id, "\n".join(lines)):
                for user_id in user_ids:
                    await self.storage.reminders.delete_deferred(hack_id, milestone, user_id)
