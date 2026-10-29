"""The four public commands and their Russian help text."""

from __future__ import annotations

from aiogram.types import BotCommand

BOT_COMMANDS: list[tuple[str, str]] = [
    ("help", "Справка"),
    ("hack", "Хакатоны"),
    ("me", "Профиль и настройки"),
    ("github", "GitHub"),
]


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command=name, description=description, is_ephemeral=True)
        for name, description in BOT_COMMANDS
    ]


HELP_TEXT = """<b>Команды бота</b>

/help — справка
/hack — хакатоны, команды и назначение GitHub issues
/me — мои задачи, аккаунт GitHub и личные настройки
/github — аккаунт GitHub и состояние интеграции

Личные действия доступны по кнопкам. Карточки, голосования и оповещения видны всему чату."""
