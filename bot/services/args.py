"""Entity-aware tokenization of command arguments and user references.

A user can be referenced in a command by:
  1. ``@username`` (looked up in the registry, re-verified via getChatMember);
  2. replying to their message with the command;
  3. a ``text_mention`` (mention picked from the member list for users without username);
  4. a numeric user_id (bot admins only);
  plus the shortcut ``me`` / ``я`` for the command author.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

from aiogram.types import Message, MessageEntity, User

from bot.errors import UserError
from bot.services.users import UserDirectory

_USERNAME_RE = re.compile(r"^@([A-Za-z0-9_]{4,32})$")
_SELF_WORDS = {"me", "я"}

REF_USERNAME = "username"
REF_TEXT_MENTION = "text_mention"
REF_ID = "id"
REF_SELF = "self"


@dataclass
class Token:
    text: str
    start: int  # index in the message text (Python str indices)
    end: int
    user: User | None = None  # set for text_mention tokens

    @property
    def ref_kind(self) -> str | None:
        if self.user is not None:
            return REF_TEXT_MENTION
        if _USERNAME_RE.match(self.text):
            return REF_USERNAME
        if self.text.lower() in _SELF_WORDS:
            return REF_SELF
        if self.text.isdigit() and len(self.text) >= 5:
            return REF_ID
        return None

    @property
    def is_explicit_user(self) -> bool:
        """A reference that can only mean a user (not an issue number or a word)."""
        return self.ref_kind in (REF_USERNAME, REF_TEXT_MENTION, REF_SELF)


@dataclass
class ParsedCommand:
    text: str
    tokens: list[Token]

    def rest(self, index: int) -> str:
        """Raw text starting at token ``index`` (keeps spaces and newlines)."""
        if index >= len(self.tokens):
            return ""
        return self.text[self.tokens[index].start :].strip()


def _utf16_to_index(text: str) -> list[int]:
    """Map UTF-16 code unit offsets to Python string indexes."""
    mapping: list[int] = []
    for idx, char in enumerate(text):
        mapping.append(idx)
        if ord(char) > 0xFFFF:
            mapping.append(idx)
    mapping.append(len(text))
    return mapping


def parse_command(message: Message) -> ParsedCommand:
    text = message.text or message.caption or ""
    entities: list[MessageEntity] = list(message.entities or message.caption_entities or [])
    offsets = _utf16_to_index(text)

    def to_idx(utf16_offset: int) -> int:
        return offsets[min(utf16_offset, len(offsets) - 1)]

    pos = 0
    mentions: dict[int, tuple[int, User]] = {}
    for entity in entities:
        start = to_idx(entity.offset)
        end = to_idx(entity.offset + entity.length)
        if entity.type == "bot_command" and start == 0:
            pos = end
        elif entity.type == "text_mention" and entity.user is not None:
            mentions[start] = (end, entity.user)

    tokens: list[Token] = []
    length = len(text)
    while pos < length:
        if pos in mentions:
            end, user = mentions[pos]
            tokens.append(Token(text[pos:end], pos, end, user))
            pos = end
            continue
        char = text[pos]
        if char.isspace():
            pos += 1
            continue
        start = pos
        parts: list[str] = []
        while pos < length and not text[pos].isspace() and pos not in mentions:
            if text[pos] == '"':
                # Shell-like quoting: label:"in progress" -> label:in progress
                close = text.find('"', pos + 1)
                if close != -1:
                    parts.append(text[pos + 1 : close])
                    pos = close + 1
                    continue
            parts.append(text[pos])
            pos += 1
        tokens.append(Token("".join(parts), start, pos))
    return ParsedCommand(text, tokens)


def reply_target(message: Message) -> User | None:
    """The author of the replied-to message, if it is a real (non-bot) user."""
    reply = message.reply_to_message
    if reply is None or reply.from_user is None:
        return None
    if reply.from_user.is_bot or reply.sender_chat is not None:
        return None
    return reply.from_user


async def resolve_user_token(
    token: Token,
    *,
    chat_id: int,
    caller_id: int,
    is_admin: bool,
    directory: UserDirectory,
) -> int:
    kind = token.ref_kind
    if kind == REF_TEXT_MENTION:
        assert token.user is not None
        await directory.remember(token.user)
        return token.user.id
    if kind == REF_USERNAME:
        return await directory.resolve_username(chat_id, token.text)
    if kind == REF_SELF:
        return caller_id
    if kind == REF_ID:
        if not is_admin:
            raise UserError("Указывать пользователя числовым id могут только администраторы бота")
        return int(token.text)
    raise UserError(
        f"«{escape(token.text)}» — не пользователь. Укажите @username, выберите человека "
        "из списка участников, ответьте командой на его сообщение или (для админов) user_id"
    )


async def resolve_user_tokens(
    tokens: list[Token],
    *,
    chat_id: int,
    caller_id: int,
    is_admin: bool,
    directory: UserDirectory,
) -> tuple[list[int], list[str]]:
    """Resolve several references. Returns (user_ids, error messages) — partial success allowed."""
    resolved: list[int] = []
    errors: list[str] = []
    for token in tokens:
        try:
            user_id = await resolve_user_token(
                token, chat_id=chat_id, caller_id=caller_id, is_admin=is_admin, directory=directory
            )
        except UserError as exc:
            errors.append(exc.message)
            continue
        if user_id not in resolved:
            resolved.append(user_id)
    return resolved, errors
