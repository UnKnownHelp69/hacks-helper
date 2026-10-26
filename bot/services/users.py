"""User registry, live membership lookups and rendering of user names.

Users are identified by Telegram user_id only. Usernames are display data that
is refreshed from getChatMember (cached for a few minutes) with the registry
as a fallback when Telegram is unreachable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Protocol

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import User

from bot.db.repositories import UsersRepo
from bot.errors import UserError

log = logging.getLogger(__name__)

IN_GROUP_STATUSES = {"creator", "administrator", "member"}
STATUS_NOT_FOUND = "not_found"


class ChatMemberApi(Protocol):
    async def get_chat_member(self, chat_id: int, user_id: int) -> Any: ...


@dataclass
class MemberInfo:
    user_id: int
    username: str | None
    first_name: str
    last_name: str | None
    status: str | None  # None means "unknown" (Telegram was unreachable)
    is_member: bool | None = None

    @property
    def full_name(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or f"id{self.user_id}"

    @property
    def in_group(self) -> bool | None:
        """True/False if known, None if membership could not be checked."""
        if self.status is None:
            return None
        if self.status in IN_GROUP_STATUSES:
            return True
        if self.status == "restricted":
            return bool(self.is_member)
        return False

    @property
    def plain(self) -> str:
        """Name without pinging: username in <code>, or the escaped full name."""
        if self.username:
            return f"<code>{escape(self.username)}</code>"
        return escape(self.full_name)

    @property
    def mention(self) -> str:
        """A pinging mention: @username, or a tg://user link for users without one."""
        if self.username:
            return f"@{escape(self.username)}"
        return f'<a href="tg://user?id={self.user_id}">{escape(self.full_name)}</a>'


class UserDirectory:
    def __init__(self, api: ChatMemberApi, users: UsersRepo, ttl_seconds: int = 300) -> None:
        self.api = api
        self.users = users
        self.ttl = ttl_seconds
        self._cache: dict[tuple[int, int], tuple[float, MemberInfo]] = {}

    async def remember(self, user: User | None) -> None:
        """Store/refresh a user seen in an update (bots are ignored)."""
        if user is None or user.is_bot:
            return
        await self.users.upsert(user.id, user.username, user.first_name, user.last_name)

    def invalidate(self, chat_id: int, user_id: int) -> None:
        self._cache.pop((chat_id, user_id), None)

    def invalidate_chat(self, chat_id: int) -> None:
        for key in [k for k in self._cache if k[0] == chat_id]:
            del self._cache[key]

    async def lookup(self, chat_id: int, user_id: int, *, force: bool = False) -> MemberInfo:
        key = (chat_id, user_id)
        cached = self._cache.get(key)
        if cached and not force and cached[0] > time.monotonic():
            return cached[1]
        try:
            member = await self.api.get_chat_member(chat_id, user_id)
        except TelegramBadRequest as exc:
            log.info("getChatMember(%s, %s) failed: %s", chat_id, user_id, exc.message)
            info = await self._from_registry(user_id, STATUS_NOT_FOUND)
        except Exception as exc:  # network errors, retry-after, bot kicked, ...
            log.warning("getChatMember(%s, %s) unavailable: %s", chat_id, user_id, exc)
            # Do not cache: try again next time.
            return await self._from_registry(user_id, None)
        else:
            user = member.user
            await self.remember(user)
            info = MemberInfo(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name or "",
                last_name=user.last_name,
                status=str(getattr(member.status, "value", member.status)),
                is_member=getattr(member, "is_member", None),
            )
        self._cache[key] = (time.monotonic() + self.ttl, info)
        return info

    async def _from_registry(self, user_id: int, status: str | None) -> MemberInfo:
        record = await self.users.get(user_id)
        if record is None:
            return MemberInfo(user_id, None, f"id{user_id}", None, status)
        return MemberInfo(user_id, record.username, record.first_name, record.last_name, status)

    async def plain(self, chat_id: int, user_id: int) -> str:
        return (await self.lookup(chat_id, user_id)).plain

    async def plain_many(self, chat_id: int, user_ids: list[int]) -> list[str]:
        return [await self.plain(chat_id, uid) for uid in user_ids]

    async def resolve_username(self, chat_id: int, username: str) -> int:
        """Find a user_id by @username via the registry, re-verified with getChatMember."""
        username = username.lstrip("@")
        record = await self.users.find_by_username(username)
        if record is None:
            raise UserError(
                f"Я ещё не знаю @{escape(username)}. Пусть он напишет в чат любое сообщение "
                "или команду /me, либо ответь командой на его сообщение"
            )
        info = await self.lookup(chat_id, record.user_id, force=True)
        if info.status is None:
            # Telegram is unreachable: fall back to the registry rather than fail.
            log.warning("Could not verify @%s via getChatMember, using registry", username)
            return record.user_id
        current = (info.username or "").lower()
        if info.status == STATUS_NOT_FOUND or current != username.lower():
            if info.status == STATUS_NOT_FOUND or not info.username:
                await self.users.clear_username(record.user_id)
            raise UserError(
                f"@{escape(username)} сейчас никому из известных участников не принадлежит"
            )
        return record.user_id
