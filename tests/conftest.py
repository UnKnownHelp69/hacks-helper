"""Shared fixtures and fakes. All ids here are synthetic test values."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetChatMember
from aiogram.types import (
    Chat,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    Message,
    MessageEntity,
    Update,
    User,
)

from bot.config import Settings
from bot.db.database import Database
from bot.db.repositories import Storage

ALLOWED_CHAT = -1001
FOREIGN_CHAT = -2002
ADMIN_ID = 1001
USER_ID = 2002
OTHER_ID = 3003


def make_settings(**overrides: Any) -> Settings:
    data: dict[str, Any] = {
        "bot_token": "test",
        "allowed_chat_ids": {ALLOWED_CHAT},
        "bot_admin_ids": frozenset({ADMIN_ID}),
        "github_token": "test-token",
        "log_file": None,
    }
    data.update(overrides)
    return Settings(**data)


def user(user_id: int, username: str | None = None, first_name: str = "Test", **kw: Any) -> User:
    return User(
        id=user_id, is_bot=kw.pop("is_bot", False), first_name=first_name, username=username, **kw
    )


def group(chat_id: int = ALLOWED_CHAT, chat_type: str = "supergroup") -> Chat:
    return Chat(id=chat_id, type=chat_type, title="Test group")


def message(
    text: str | None = None,
    *,
    chat: Chat | None = None,
    from_user: User | None = None,
    entities: list[MessageEntity] | None = None,
    **kw: Any,
) -> Message:
    if text is not None and text.startswith("/") and entities is None:
        command = text.split()[0]
        entities = [
            MessageEntity(
                type="bot_command", offset=0, length=len(command.encode("utf-16-le")) // 2
            )
        ]
    return Message(
        message_id=kw.pop("message_id", 1),
        date=datetime(2026, 10, 26, tzinfo=UTC),
        chat=chat or group(),
        from_user=from_user if from_user is not None else user(USER_ID, "tester"),
        text=text,
        entities=entities,
        **kw,
    )


def update_with(message_obj: Message | None = None, **kw: Any) -> Update:
    return Update(update_id=1, message=message_obj, **kw)


class FakeBot:
    """Implements the few Bot API calls the services use."""

    def __init__(self) -> None:
        self.members: dict[tuple[int, int], Any] = {}
        self.sent: list[tuple[int, str]] = []
        self.left: list[int] = []
        self.get_member_calls = 0
        self.network_down = False

    def set_member(self, chat_id: int, u: User, status: str = "member") -> None:
        if status == "member":
            self.members[(chat_id, u.id)] = ChatMemberMember(user=u)
        elif status == "left":
            self.members[(chat_id, u.id)] = ChatMemberLeft(user=u)
        elif status == "kicked":
            self.members[(chat_id, u.id)] = ChatMemberBanned(user=u, until_date=0)
        else:
            raise ValueError(status)

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.get_member_calls += 1
        if self.network_down:
            raise TelegramNetworkError(
                method=GetChatMember(chat_id=chat_id, user_id=user_id), message="down"
            )
        try:
            return self.members[(chat_id, user_id)]
        except KeyError:
            raise TelegramBadRequest(
                method=GetChatMember(chat_id=chat_id, user_id=user_id),
                message="Bad Request: user not found",
            ) from None

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.sent.append((chat_id, text))

    async def leave_chat(self, chat_id: int) -> bool:
        self.left.append(chat_id)
        return True


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
async def storage():
    db = Database(":memory:")
    await db.connect()
    yield Storage(db, "Europe/Moscow")
    await db.close()
