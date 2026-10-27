"""Level 1 and level 2 protection, applied before any handler runs.

* Updates from chats that are not in ALLOWED_CHAT_IDS are dropped; if the bot
  finds itself in such a chat it leaves it without writing anything.
* In private chats only /me is processed unless the chat is explicitly allowed.
* Setup mode (ALLOWED_CHAT_IDS empty): only /help in groups and /me in
  private chats are processed, and the bot leaves no chats.
* Bot admin status is computed here, centrally, from the user_id only.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, Update, User

from bot.config import Settings
from bot.respond import personal_reply

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {"member", "administrator", "creator", "restricted"}

SENDER_CHAT_REFUSAL = "Пишите команды от своего имени, не от имени группы или канала"
ADMIN_ONLY_TEXT = "Эта команда доступна только администраторам бота"

MigrateCallback = Callable[[int, int], Awaitable[None]]


class LeaveApi(Protocol):
    async def leave_chat(self, chat_id: int) -> Any: ...


def status_value(status: Any) -> str:
    return str(getattr(status, "value", status))


def command_name(message: Message) -> str | None:
    """'/cmd@bot args' -> 'cmd' (lowercase), None if the message is not a command."""
    text = message.text or message.caption or ""
    if not text.startswith("/"):
        return None
    head = text.split(maxsplit=1)[0][1:]
    return head.split("@", 1)[0].lower() or None


def update_chat_and_user(update: Update) -> tuple[Chat | None, User | None]:
    if update.message:
        return update.message.chat, update.message.from_user
    if update.edited_message:
        return update.edited_message.chat, update.edited_message.from_user
    if update.callback_query:
        msg = update.callback_query.message
        return (msg.chat if msg else None), update.callback_query.from_user
    if update.chat_member:
        return update.chat_member.chat, update.chat_member.from_user
    if update.my_chat_member:
        return update.my_chat_member.chat, update.my_chat_member.from_user
    return None, None


def is_bot_admin(settings: Settings, event: Message | CallbackQuery) -> bool:
    """Bot admin = allowed chat AND from_user.id in BOT_ADMIN_IDS AND not sent on behalf of a chat.

    Telegram group admin rights are irrelevant. Anonymous admins / channels
    (sender_chat) are never admins.
    """
    if isinstance(event, CallbackQuery):
        message = event.message
        chat = message.chat if message else None
        user = event.from_user
        sender_chat = None
    else:
        chat = event.chat
        user = event.from_user
        sender_chat = event.sender_chat
    if chat is None or user is None or user.is_bot or sender_chat is not None:
        return False
    if not settings.is_chat_allowed(chat.id):
        return False
    return settings.is_admin_id(user.id)


class SecurityMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, on_migrate: MigrateCallback | None = None) -> None:
        self.settings = settings
        self.on_migrate = on_migrate
        self._left_chats: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        assert isinstance(event, Update)
        if not await self.check(event, data["bot"]):
            return None
        data["is_admin"] = self._admin_flag(event)
        return await handler(event, data)

    def _admin_flag(self, update: Update) -> bool:
        if update.message:
            return is_bot_admin(self.settings, update.message)
        if update.callback_query:
            return is_bot_admin(self.settings, update.callback_query)
        return False

    async def _leave(self, bot: LeaveApi, chat: Chat, reason: str) -> None:
        log.warning(
            "Leaving chat %s (%s, '%s'): %s. If this is your group, add %s to ALLOWED_CHAT_IDS.",
            chat.id,
            chat.type,
            chat.title,
            reason,
            chat.id,
        )
        try:
            await bot.leave_chat(chat.id)
        except Exception as exc:
            log.warning("leaveChat(%s) failed: %s", chat.id, exc)
        self._left_chats.add(chat.id)

    async def check(self, update: Update, bot: Any) -> bool:
        """Return True if the update may reach handlers."""
        settings = self.settings
        if update.callback_query and update.callback_query.message:
            receiver = getattr(update.callback_query.message, "receiver_user", None)
            if receiver and receiver.id != update.callback_query.from_user.id:
                return False

        if update.my_chat_member:
            event = update.my_chat_member
            chat = event.chat
            if chat.type == ChatType.PRIVATE:
                return False
            added = status_value(event.new_chat_member.status) in ACTIVE_STATUSES
            if settings.setup_mode:
                if added:
                    log.warning(
                        "Setup mode: bot added to chat %s ('%s') by user %s. "
                        "Send /help there to get its id for ALLOWED_CHAT_IDS.",
                        chat.id,
                        chat.title,
                        event.from_user.id if event.from_user else None,
                    )
                return False
            if settings.is_chat_allowed(chat.id):
                return True
            if added:
                by = event.from_user.id if event.from_user else None
                await self._leave(bot, chat, f"added by user {by}, chat is not allowed")
            return False

        chat, _user = update_chat_and_user(update)
        if chat is None:
            return False
        message = update.message

        if chat.type == ChatType.PRIVATE:
            return settings.is_chat_allowed(chat.id) or (
                message is not None and command_name(message) == "me"
            )

        # Group -> supergroup migration of an allowed chat.
        if message is not None and not settings.setup_mode:
            if message.migrate_from_chat_id and settings.is_chat_allowed(
                message.migrate_from_chat_id
            ):
                await self._migrate(message.migrate_from_chat_id, chat.id)
                return False
            if message.migrate_to_chat_id and settings.is_chat_allowed(chat.id):
                await self._migrate(chat.id, message.migrate_to_chat_id)
                return False

        if settings.setup_mode:
            return message is not None and command_name(message) == "help"

        if not settings.is_chat_allowed(chat.id):
            if chat.id not in self._left_chats and chat.type != ChatType.PRIVATE:
                await self._leave(bot, chat, "update from a chat that is not allowed")
            return False

        if message is not None and message.sender_chat is not None:
            # Anonymous admins and channel posts: never treated as users.
            if command_name(message) and message.from_user and not message.from_user.is_bot:
                try:
                    await personal_reply(message, SENDER_CHAT_REFUSAL)
                except Exception as exc:
                    log.debug("Could not reply to a sender_chat message: %s", exc)
            return False
        return True

    async def _migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        # Both the old group and the new supergroup receive a service message;
        # handle the migration only once.
        already_known = new_chat_id in self.settings.allowed_chat_ids
        self.settings.allowed_chat_ids.add(new_chat_id)
        if self.on_migrate is not None and not already_known:
            await self.on_migrate(old_chat_id, new_chat_id)
