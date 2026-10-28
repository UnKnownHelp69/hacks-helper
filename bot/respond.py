"""Personal replies: native ephemeral group messages, ordinary replies in private chats."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    EphemeralMessageParameters,
    ForceReply,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
)

from bot.errors import UserError
from bot.services.formatting import split_message

if TYPE_CHECKING:
    from bot.context import AppContext

POPUP_LIMIT = 200
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class PersonalPanel:
    message_id: int | None = None
    ephemeral_id: int | None = None
    revision: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Keep only identifiers, not Message/Bot references. Each bot and each recipient is isolated.
_PANELS: WeakKeyDictionary[BaseSession, dict[tuple[int, int, int], PersonalPanel]] = (
    WeakKeyDictionary()
)


def _panel(event: Message | CallbackQuery) -> PersonalPanel:
    message = event.message if isinstance(event, CallbackQuery) else event
    # Bot equality compares tokens, so use the actual API session identity as the weak key.
    panels = _PANELS.setdefault(event.bot.session, {})
    return panels.setdefault((event.bot.id, message.chat.id, event.from_user.id), PersonalPanel())


def _panel_message(event: Message | CallbackQuery, panel: PersonalPanel) -> Message | None:
    if panel.message_id is None:
        return None
    message = event.message if isinstance(event, CallbackQuery) else event
    return Message(
        message_id=panel.message_id,
        ephemeral_message_id=panel.ephemeral_id,
        receiver_user=event.from_user if panel.ephemeral_id is not None else None,
        date=datetime.now(UTC),
        chat=message.chat,
    )


def _remember_panel(event: Message | CallbackQuery, sent: Message) -> None:
    panel = _panel(event)
    panel.message_id, panel.ephemeral_id = sent.message_id, sent.ephemeral_message_id


def personal_panel_revision(event: Message | CallbackQuery) -> int:
    """Background progress yields the panel when a newer interaction replaces its contents."""
    return _panel(event).revision


def html_to_plain(text: str) -> str:
    return unescape(_TAG_RE.sub("", text)).strip()


def fits_popup(text: str) -> bool:
    plain = html_to_plain(text)
    return 0 < len(plain.encode("utf-16-le")) // 2 <= POPUP_LIMIT


def personal_options(event: Message | CallbackQuery, *, overlay: bool = False) -> dict[str, Any]:
    message = event.message if isinstance(event, CallbackQuery) else event
    user = event.from_user
    if not isinstance(message, Message) or user is None or user.is_bot:
        raise UserError("Личный ответ недоступен. Отправьте /hack от своего имени")
    options: dict[str, Any] = {"chat_id": message.chat.id}
    if message.chat.type in {"group", "supergroup"}:
        if (
            isinstance(event, CallbackQuery)
            and message.receiver_user
            and message.receiver_user.id != user.id
        ):
            raise UserError("Это личное сообщение другого пользователя")
        options["ephemeral_message_parameters"] = EphemeralMessageParameters(
            receiver_user_id=user.id,
            callback_query_id=event.id if isinstance(event, CallbackQuery) else None,
            replace_callback_query_message=(
                overlay and message.ephemeral_message_id is None
                if isinstance(event, CallbackQuery)
                else False
            ),
        )
        if (
            isinstance(event, Message)
            and message.ephemeral_message_id is not None
            and (datetime.now(UTC) - message.date).total_seconds() < 12
        ):
            options["reply_parameters"] = ReplyParameters(
                ephemeral_message_id=message.ephemeral_message_id, allow_sending_without_reply=False
            )
    return options


async def personal_reply(
    event: Message | CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | ForceReply | None = None,
    edit: bool = True,
) -> Message | bool:
    """Never retry a failed private delivery as a public group message."""
    options = personal_options(event, overlay=edit and not isinstance(reply_markup, ForceReply))
    chunks = split_message(text)
    panel = _panel(event)
    async with panel.lock:
        panel.revision += 1
        if edit and len(chunks) == 1 and not isinstance(reply_markup, ForceReply):
            target = _panel_message(event, panel)
            if target is None and isinstance(event, CallbackQuery):
                message = event.message
                if message.ephemeral_message_id is not None or message.chat.type == "private":
                    target = message
            if target is not None:
                try:
                    await edit_personal_reply(event, target, text, reply_markup=reply_markup)
                    _remember_panel(event, target)
                    return target.model_copy(update={"text": text, "reply_markup": reply_markup})
                except TelegramBadRequest as exc:
                    if not any(
                        marker in exc.message.lower()
                        for marker in (
                            "not found",
                            "can't be edited",
                            "expired",
                            "message_id_invalid",
                        )
                    ):
                        raise
        result: Message | bool = False
        for index, chunk in enumerate(chunks):
            result = await event.bot.send_message(
                **options,
                text=chunk,
                parse_mode="HTML",
                link_preview_options=NO_PREVIEW,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )
        if isinstance(result, Message):
            _remember_panel(event, result)
        return result


async def reply(message: Message, text: str) -> None:
    await personal_reply(message, text)


async def edit_personal_reply(
    event: Message | CallbackQuery,
    sent: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | bool:
    """Edit a bot-owned progress panel, retaining the original personal recipient."""
    options = personal_options(event)
    message = event.message if isinstance(event, CallbackQuery) else event
    if sent.chat.id != message.chat.id:
        raise UserError("Личное сообщение относится к другому чату")
    if message.chat.type in {"group", "supergroup"}:
        if (
            sent.ephemeral_message_id is None
            or sent.receiver_user is None
            or sent.receiver_user.id != event.from_user.id
        ):
            raise UserError("Это личное сообщение другого пользователя")
        method = event.bot.edit_ephemeral_message_text(
            chat_id=message.chat.id,
            receiver_user_id=event.from_user.id,
            ephemeral_message_id=sent.ephemeral_message_id,
            text=text,
            parse_mode="HTML",
            link_preview_options=NO_PREVIEW,
            reply_markup=reply_markup,
        )
    else:
        method = event.bot.edit_message_text(
            chat_id=options["chat_id"],
            message_id=sent.message_id,
            text=text,
            parse_mode="HTML",
            link_preview_options=NO_PREVIEW,
            reply_markup=reply_markup,
        )
    try:
        return await method
    except TelegramBadRequest as exc:
        if "not modified" in exc.message.lower():
            return True
        raise


async def respond(
    message: Message, text: str, ctx: AppContext, *, personal: str | None = None
) -> None:
    await personal_reply(message, text)
