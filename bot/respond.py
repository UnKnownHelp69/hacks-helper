"""Personal replies: native ephemeral group messages, ordinary replies in private chats."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from typing import TYPE_CHECKING, Any

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
    message = event.message if isinstance(event, CallbackQuery) else event
    chunks = split_message(text)
    if (
        isinstance(event, CallbackQuery)
        and edit
        and len(chunks) == 1
        and not isinstance(reply_markup, ForceReply)
    ):
        try:
            if message.ephemeral_message_id is not None:
                return await event.bot.edit_ephemeral_message_text(
                    chat_id=message.chat.id,
                    receiver_user_id=event.from_user.id,
                    ephemeral_message_id=message.ephemeral_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    link_preview_options=NO_PREVIEW,
                )
            if message.chat.type == "private":
                return await event.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    link_preview_options=NO_PREVIEW,
                )
        except TelegramBadRequest as exc:
            if "not modified" in exc.message.lower():
                return True
            if (
                "not found" not in exc.message.lower()
                and "can't be edited" not in exc.message.lower()
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
    return result


async def reply(message: Message, text: str) -> None:
    await personal_reply(message, text)


async def edit_personal_reply(
    event: Message,
    sent: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | bool:
    """Edit a bot-owned progress panel, retaining the original personal recipient."""
    options = personal_options(event)
    if sent.chat.id != event.chat.id:
        raise UserError("Личное сообщение относится к другому чату")
    if event.chat.type in {"group", "supergroup"}:
        if (
            sent.ephemeral_message_id is None
            or sent.receiver_user is None
            or sent.receiver_user.id != event.from_user.id
        ):
            raise UserError("Это личное сообщение другого пользователя")
        method = event.bot.edit_ephemeral_message_text(
            chat_id=event.chat.id,
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
