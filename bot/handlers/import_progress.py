"""A single personal progress panel for a long-running hackathon import."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from html import escape
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from bot.respond import edit_personal_reply, personal_reply
from bot.services.hack_data import DATE_FIELDS, FIELD_LABELS
from bot.services.timeutil import format_dt

log = logging.getLogger(__name__)


class ImportProgress:
    def __init__(self, message: Message, tz: ZoneInfo) -> None:
        self.message = message
        self.tz = tz
        self.panel: Message | None = None
        self.stage = "Начинаю обработку"
        self.data: dict[str, Any] = {}
        self.started = monotonic()
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.failed = False
        self.last_render = self.started - 2

    async def __aenter__(self) -> ImportProgress:
        return self

    async def __aexit__(self, *args) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def update(self, stage: str, data: dict[str, Any]) -> None:
        self.stage, self.data = stage, data
        await self.render()
        if self.task is None and self.panel:
            self.task = asyncio.create_task(self.tick())

    async def tick(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self.render()

    async def render(self) -> None:
        async with self.lock:
            if self.failed:
                return
            now = monotonic()
            if self.panel is not None and now - self.last_render < 1:
                return  # Coalesce fast stages; the heartbeat renders the latest snapshot.
            elapsed = int(now - self.started)
            spinner = ("◐", "◓", "◑", "◒")[(elapsed // 5) % 4]
            lines = [
                f"{spinner} <b>Изучаю хакатон</b> · {elapsed // 60}:{elapsed % 60:02d}",
                escape(self.stage[:220]),
                "Обработка может занять до двух минут. Данные уточняются по мере чтения.",
                "",
            ]
            for key, label in FIELD_LABELS.items():
                value = self.data.get(key)
                if key in DATE_FIELDS and value:
                    value = format_dt(datetime.fromisoformat(value), self.tz)
                lines.append(
                    f"<b>{label}:</b> "
                    + (escape(str(value)[:130]) if value is not None else "ищу…")
                )
            lines.append(f"Чекпоинты: {len(self.data.get('checkpoints', []))}")
            text = "\n".join(lines)
            if self.panel is None:
                sent = await personal_reply(self.message, text, edit=False)
                if isinstance(sent, Message):
                    self.panel = sent
                self.last_render = now
                return
            try:
                await edit_personal_reply(self.message, self.panel, text)
                self.last_render = now
            except TelegramAPIError:
                # Import can still finish and persist its draft if an ephemeral panel expires.
                log.warning("Hackathon import progress panel is unavailable")
                self.panel, self.failed = None, True
