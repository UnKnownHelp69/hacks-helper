"""Shared voting cards and retryable team/archive announcements."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from html import escape, unescape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    User,
)

from bot.config import Settings
from bot.db.models import Hackathon
from bot.db.repositories import Storage
from bot.respond import personal_reply
from bot.services.formatting import link
from bot.services.notify import Messenger
from bot.services.reminders import chat_zone
from bot.services.timeutil import format_dt
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
        votes = await self.storage.workflow.votes(hack.id)
        teams = await self.storage.workflow.teams(hack.id)
        lines = [
            f"🏆 <b>{escape(hack.display_name)}</b>",
            f"Регистрация до: {format_dt(hack.registration_end, tz)}",
            f"Начало: {format_dt(hack.start_at, tz)}",
            f"Конец: {format_dt(hack.end_at, tz)} · {escape(tz.key)}",
            f"Команда: {hack.team_min}–{hack.team_max} участников",
        ]
        for url, label in (
            (hack.site_url, "Сайт хакатона"),
            (f"https://github.com/{hack.repo}" if hack.repo else None, "Репозиторий решения"),
            (hack.org_chat_url, "Чат организаторов"),
            (hack.spec_url, "ТЗ"),
        ):
            if url:
                lines.append(link(url, label))
        lines.append(
            f"\nГолосование: ✅ {len(votes['yes'])} · ❌ {len(votes['no'])} "
            f"· 🤔 {len(votes['maybe'])}"
        )
        for team in teams[:10]:
            names = []
            for user_id in team.members[:8]:
                name = unescape(await self.directory.plain(hack.chat_id, user_id))
                names.append(escape(name[:20]) + ("…" if len(name) > 20 else ""))
            if len(team.members) > 8:
                names.append(f"ещё {len(team.members) - 8}")
            state = "готова" if len(team.members) >= hack.team_min else "набор"
            if len(team.members) > hack.team_max:
                state += ", сверх лимита"
            lines.append(
                f"👥 {escape(team.name)}: {len(team.members)}/{hack.team_max} ({state})"
                + (" — " + ", ".join(names) if names else "")
            )
        if len(teams) > 10:
            lines.append(f"Ещё команд: {len(teams) - 10}")
        checkpoints = await self.storage.workflow.checkpoints(hack.id)
        for checkpoint in checkpoints[:5]:
            lines.append(f"📍 {escape(checkpoint.title[:40])}: {format_dt(checkpoint.at, tz)}")
        if len(checkpoints) > 5:
            lines.append(f"Ещё чекпоинтов: {len(checkpoints) - 5}")
        rows = []
        if hack.is_active:
            rows.append(
                [
                    InlineKeyboardButton(text="✅ Участвую", callback_data=f"hv:{hack.id}:yes"),
                    InlineKeyboardButton(text="❌ Нет", callback_data=f"hv:{hack.id}:no"),
                    InlineKeyboardButton(text="🤔 Думаю", callback_data=f"hv:{hack.id}:maybe"),
                ]
            )
        else:
            lines.append("🏁 Хакатон завершён")
        rows.append(
            [InlineKeyboardButton(text="Мой выбор и команды", callback_data=f"hm:{hack.id}")]
        )
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    async def flush(self) -> None:
        async with self._lock:
            for publication in await self.storage.workflow.dirty_publications():
                hack = await self.storage.hackathons.get_by_id(publication["hackathon_id"])
                if hack is None or not self.settings.is_chat_allowed(hack.chat_id):
                    continue
                try:
                    text, keyboard = await self.card(hack)
                    if hack.chat_id > 0:
                        origin = Message(
                            message_id=0,
                            date=datetime.now(UTC),
                            chat=Chat(id=hack.chat_id, type="private"),
                            from_user=User(id=hack.chat_id, is_bot=False, first_name="User"),
                        ).as_(self.api)
                        sent = await personal_reply(origin, text, reply_markup=keyboard)
                        if not isinstance(sent, Message):
                            raise RuntimeError("Private hackathon panel was not delivered")
                        await self.storage.workflow.publication_sent(
                            hack.id, sent.message_id, publication["dirty"]
                        )
                        continue
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
