"""Shared voting cards and retryable team/archive announcements."""

from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)

from bot.config import Settings
from bot.db.database import utcnow
from bot.db.models import Hackathon
from bot.db.repositories import Storage
from bot.services.formatting import link
from bot.services.notify import Messenger
from bot.services.reminders import chat_zone
from bot.services.schedule_logic import current_phase
from bot.services.timeutil import format_dt, tz_label
from bot.services.users import UserDirectory

log = logging.getLogger(__name__)


class HackNotifications:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        api: Bot,
        directory: UserDirectory,
        messenger: Messenger,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.api = api
        self.directory = directory
        self.messenger = messenger
        self._lock = asyncio.Lock()

    async def card(self, hack: Hackathon) -> tuple[str, InlineKeyboardMarkup]:
        tz = await chat_zone(self.storage, hack.chat_id, self.settings.default_timezone)
        members = await self.storage.hackathons.members(hack.id)
        phase = current_phase(hack, utcnow()) if hack.is_active else "хакатон завершён"
        phase_icon = {
            "идёт регистрация": "🟢",
            "регистрация закрыта, ждём старта": "⏳",
            "хакатон идёт": "🚀",
            "хакатон завершён": "🏁",
        }[phase]
        lines = [
            f"🏆 <b>{escape(hack.display_name)}</b>",
            f"{phase_icon} <b>{phase[0].upper() + phase[1:]}</b>",
            "",
            f"📅 <b>Сроки</b> · {escape(tz_label(tz))}",
            f"Регистрация до: <b>{format_dt(hack.registration_end, tz)}</b>",
            f"Начало: <b>{format_dt(hack.start_at, tz)}</b>",
            f"Конец: <b>{format_dt(hack.end_at, tz)}</b>",
            "",
            f"👥 <b>Команда</b> · до {hack.team_max} чел.",
            f"Участники: {len(members)}",
        ]
        materials = []
        for url, label in (
            (hack.site_url, "Сайт"),
            (f"https://github.com/{hack.repo}" if hack.repo else None, "GitHub"),
            (hack.org_chat_url, "Чат"),
            (hack.spec_url, "ТЗ"),
        ):
            if url:
                materials.append(link(url, label))
        if materials:
            lines.extend(["", "🔗 " + " · ".join(materials)])
        checkpoints = await self.storage.workflow.checkpoints(hack.id)
        rows = []
        footer = [InlineKeyboardButton(text="Открыть", callback_data=f"hm:{hack.id}")]
        if checkpoints:
            footer.append(InlineKeyboardButton(text="📍 Чекпоинты", callback_data=f"hc:{hack.id}"))
        rows.append(footer)
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    async def flush(self) -> None:
        async with self._lock:
            for publication in await self.storage.workflow.dirty_publications():
                hack = await self.storage.hackathons.get_by_id(publication["hackathon_id"])
                if hack is None or not self.settings.is_chat_allowed(hack.chat_id):
                    continue
                try:
                    text, keyboard = await self.card(hack)
                    message_id = publication["message_id"]
                    if message_id:
                        try:
                            await self.api.edit_message_text(
                                chat_id=hack.chat_id,
                                message_id=message_id,
                                text=text,
                                reply_markup=keyboard,
                                parse_mode="HTML",
                                link_preview_options=LinkPreviewOptions(is_disabled=True),
                            )
                        except TelegramBadRequest as exc:
                            reason = exc.message.lower()
                            if "not modified" in reason:
                                pass
                            elif (
                                "message to edit not found" in reason or "can't be edited" in reason
                            ):
                                message_id = None
                            else:
                                raise
                    if not message_id:
                        message = await self.api.send_message(
                            hack.chat_id,
                            text,
                            reply_markup=keyboard,
                            parse_mode="HTML",
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                        message_id = message.message_id
                    await self.storage.workflow.publication_sent(
                        hack.id, message_id, publication["dirty"]
                    )
                except Exception:
                    log.exception(
                        "Shared hackathon card %s could not be updated; will retry", hack.id
                    )
            for notice in await self.storage.workflow.pending_notices():
                hack = await self.storage.hackathons.get_by_id(notice["hackathon_id"])
                if (
                    hack
                    and self.settings.is_chat_allowed(hack.chat_id)
                    and await self.messenger.send(hack.chat_id, notice["text"])
                ):
                    await self.storage.workflow.notice_sent(notice["id"])
