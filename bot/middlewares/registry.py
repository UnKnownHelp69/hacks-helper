"""Keeps the user registry (user_id -> last known username/name) up to date."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update, User

from bot.services.users import UserDirectory

log = logging.getLogger(__name__)


def users_from_message(message: Message) -> list[User]:
    users: list[User] = []
    if message.from_user and message.sender_chat is None:
        users.append(message.from_user)
    reply = message.reply_to_message
    if reply and reply.from_user and reply.sender_chat is None:
        users.append(reply.from_user)
    users.extend(message.new_chat_members or [])
    if message.left_chat_member:
        users.append(message.left_chat_member)
    for entity in [*(message.entities or []), *(message.caption_entities or [])]:
        if entity.type == "text_mention" and entity.user is not None:
            users.append(entity.user)
    return users


def users_from_update(update: Update) -> list[User]:
    users: list[User] = []
    for message in (update.message, update.edited_message):
        if message is not None:
            users.extend(users_from_message(message))
    if update.callback_query:
        users.append(update.callback_query.from_user)
    if update.chat_member:
        users.append(update.chat_member.new_chat_member.user)
        if update.chat_member.from_user:
            users.append(update.chat_member.from_user)
    return [u for u in users if not u.is_bot]


class UserRegistryMiddleware(BaseMiddleware):
    def __init__(self, directory: UserDirectory) -> None:
        self.directory = directory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        assert isinstance(event, Update)
        try:
            await self.observe(event)
        except Exception:
            log.exception("Failed to update the user registry")
        return await handler(event, data)

    async def observe(self, update: Update) -> None:
        seen: set[int] = set()
        for user in users_from_update(update):
            if user.id in seen:
                continue
            seen.add(user.id)
            await self.directory.remember(user)
        # Membership changes invalidate the cached getChatMember result.
        chat_id = None
        changed: list[int] = []
        if update.chat_member:
            chat_id = update.chat_member.chat.id
            changed.append(update.chat_member.new_chat_member.user.id)
        elif update.message and (
            update.message.new_chat_members or update.message.left_chat_member
        ):
            chat_id = update.message.chat.id
            changed.extend(u.id for u in update.message.new_chat_members or [])
            if update.message.left_chat_member:
                changed.append(update.message.left_chat_member.id)
        if chat_id is not None:
            for user_id in changed:
                self.directory.invalidate(chat_id, user_id)
