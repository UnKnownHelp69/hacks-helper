"""Sending messages to the group and deciding whom it is OK to ping."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from aiogram.types import EphemeralMessageParameters, LinkPreviewOptions

from bot.db.repositories import AvailabilityRepo
from bot.services.availability import deserialize_schedule, is_available, next_available
from bot.services.formatting import split_message
from bot.services.users import UserDirectory

log = logging.getLogger(__name__)


class MessageApi(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any: ...


class Messenger:
    """Sends HTML messages to chats; never raises (errors are logged)."""

    def __init__(self, api: MessageApi) -> None:
        self.api = api

    async def send(self, chat_id: int, text: str, *, receiver_user_id: int | None = None) -> bool:
        options: dict[str, Any] = {}
        if receiver_user_id is not None:
            if chat_id > 0 and chat_id != receiver_user_id:
                return False
            if chat_id < 0:
                options["ephemeral_message_parameters"] = EphemeralMessageParameters(
                    receiver_user_id=receiver_user_id
                )
        ok = True
        for chunk in split_message(text):
            try:
                await self.api.send_message(
                    chat_id,
                    chunk,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    **options,
                )
            except Exception:
                log.exception("Failed to send a message to chat %s", chat_id)
                ok = False
        return ok


@dataclass
class PingPlan:
    mentions: list[str] = field(default_factory=list)  # users to ping now (rendered)
    pinged_ids: list[int] = field(default_factory=list)
    not_in_group: list[str] = field(default_factory=list)  # rendered without ping
    deferred: dict[int, datetime] = field(default_factory=dict)  # user_id -> next window (UTC)
    deferred_names: list[str] = field(default_factory=list)
    skipped_names: list[str] = field(default_factory=list)  # no window before the deadline

    def render_lines(self) -> list[str]:
        lines = []
        if self.mentions:
            lines.append("👥 " + ", ".join(self.mentions))
        if self.deferred_names:
            lines.append("⏸ Позже: " + ", ".join(self.deferred_names))
        if self.skipped_names:
            lines.append("🔕 Без пинга: " + ", ".join(self.skipped_names))
        lines.extend(f"🚪 {name} нет в группе" for name in self.not_in_group)
        return lines


class Pinger:
    def __init__(self, directory: UserDirectory, availability: AvailabilityRepo) -> None:
        self.directory = directory
        self.availability = availability

    async def user_available(self, user_id: int, tz: ZoneInfo, now: datetime) -> bool:
        schedule = deserialize_schedule(await self.availability.get_raw(user_id))
        return is_available(schedule, now.astimezone(tz))

    async def plan(
        self,
        chat_id: int,
        user_ids: list[int],
        *,
        tz: ZoneInfo,
        now: datetime,
        deadline: datetime | None = None,
    ) -> PingPlan:
        """Check membership (fresh getChatMember) and availability before pinging.

        With ``deadline`` set, unavailable users get a deferred ping at their next
        allowed window if it starts before the deadline, otherwise they are skipped.
        """
        plan = PingPlan()
        for user_id in user_ids:
            info = await self.directory.lookup(chat_id, user_id, force=True)
            if info.in_group is False:
                plan.not_in_group.append(info.plain)
                continue
            schedule = deserialize_schedule(await self.availability.get_raw(user_id))
            local_now = now.astimezone(tz)
            if is_available(schedule, local_now):
                plan.mentions.append(info.mention)
                plan.pinged_ids.append(user_id)
                continue
            nxt = next_available(schedule, local_now) if deadline is not None else None
            if nxt is not None and deadline is not None and nxt < deadline:
                plan.deferred[user_id] = nxt
                plan.deferred_names.append(info.plain)
            else:
                plan.skipped_names.append(info.plain)
        return plan

    async def render_user(self, chat_id: int, user_id: int, tz: ZoneInfo, now: datetime) -> str:
        """Mention for notifications: pings only if the user is in the group and available."""
        info = await self.directory.lookup(chat_id, user_id)
        if info.in_group is False:
            return f"{info.plain} (нет в группе)"
        if await self.user_available(user_id, tz, now):
            return info.mention
        return info.plain
