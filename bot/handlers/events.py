"""Chat events (bot status changes) and the global error handler."""

from __future__ import annotations

import contextlib
import logging

from aiogram import Router
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import ChatMemberUpdated, ErrorEvent, Message

from bot.errors import UserError
from bot.middlewares.security import status_value

log = logging.getLogger(__name__)

router = Router(name="events")

NOT_ADMIN_WARNING = (
    "⚠️ Я не администратор этой группы. Сделайте меня администратором "
    "без дополнительных прав (все переключатели выключены): иначе я не смогу надёжно "
    "проверять участников и узнавать об их входе и выходе."
)


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    """Only allowed chats reach this handler (others are left by SecurityMiddleware)."""
    status = status_value(event.new_chat_member.status)
    log.info("Bot status in chat %s changed to %s", event.chat.id, status)
    if status in {"member", "restricted"}:
        try:
            await event.answer(NOT_ADMIN_WARNING)
        except Exception as exc:
            log.warning("Could not warn chat %s: %s", event.chat.id, exc)
    elif status == "administrator":
        try:
            await event.answer("✅ Права администратора получены. Справка: /help")
        except Exception as exc:
            log.warning("Could not notify chat %s: %s", event.chat.id, exc)


def _event_message(event: ErrorEvent) -> Message | None:
    update = event.update
    if update.message:
        return update.message
    if update.callback_query and isinstance(update.callback_query.message, Message):
        return update.callback_query.message
    return None


@router.error(ExceptionTypeFilter(UserError))
async def on_user_error(event: ErrorEvent) -> None:
    assert isinstance(event.exception, UserError)
    text = f"❌ {event.exception.message}"
    if event.update.callback_query:
        with contextlib.suppress(Exception):
            await event.update.callback_query.answer()
    message = _event_message(event)
    if message is None:
        return
    try:
        if event.update.message:
            await message.reply(text)
        else:
            await message.answer(text)
    except Exception as exc:
        log.warning("Could not deliver an error message: %s", exc)


@router.error()
async def on_unexpected_error(event: ErrorEvent) -> None:
    log.error(
        "Unhandled error while processing update %s",
        event.update.update_id,
        exc_info=event.exception,
    )
    message = _event_message(event)
    if message is None:
        return
    try:
        await message.answer("⚠️ Внутренняя ошибка. Подробности записаны в лог бота.")
    except Exception as exc:
        log.warning("Could not deliver an error message: %s", exc)
