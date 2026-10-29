"""GitHub account and integration status through /github."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.context import AppContext
from bot.respond import reply
from bot.services.formatting import link

router = Router(name="github")


@router.message(Command("github"))
async def cmd_github(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    await ctx.storage.workflow.clear_forms(message.from_user.id, message.chat.id)
    login = await ctx.storage.github_links.get_login(message.from_user.id)
    account = link(f"https://github.com/{login}", login) if login else "не привязан"
    status = "настроена" if ctx.github.configured else "не настроена"
    await reply(
        message,
        f"🐙 <b>GitHub</b>\nАккаунт: {account}\nИнтеграция: {status}\n"
        "Привязка аккаунта: /me → Аккаунт GitHub\n"
        "Назначение issues: /hack → хакатон → Задачи",
    )
