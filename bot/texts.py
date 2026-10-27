"""The four public commands and their Russian help text."""

from __future__ import annotations

from aiogram.types import BotCommand

BOT_COMMANDS: list[tuple[str, str]] = [
    ("help", "Справка"),
    ("hack", "Хакатоны"),
    ("me", "Мой профиль"),
    ("github", "GitHub"),
]


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command=name, description=description) for name, description in BOT_COMMANDS]


HELP_TEXT = """<b>Команды бота</b>

/help — справка
/hack — список хакатонов; /hack &lt;название&gt; — карточка
/me — мой профиль
/github — аккаунт GitHub и состояние интеграции"""
