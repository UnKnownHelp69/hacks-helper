"""Guard for routers whose every handler is bot-admin only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.middlewares.security import ADMIN_ONLY_TEXT


class AdminOnlyMiddleware(BaseMiddleware):
    """Inner middleware: runs after a handler matched, before it executes.

    ``is_admin`` is computed centrally by SecurityMiddleware from the user_id.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if data.get("is_admin") is True:
            return await handler(event, data)
        if isinstance(event, Message):
            await event.reply(ADMIN_ONLY_TEXT)
        elif isinstance(event, CallbackQuery):
            await event.answer(ADMIN_ONLY_TEXT, show_alert=True)
        return None
