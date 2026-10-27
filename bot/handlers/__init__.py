"""Router assembly for the four public commands and answer buttons."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType

from bot.handlers import (
    common,
    events,
    github,
    hackathons,
    popups,
)


def build_router() -> Router:
    root = Router(name="root")
    root.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE}))
    root.include_routers(
        events.router,
        popups.router,
        common.router,
        hackathons.router,
        github.router,
    )
    return root
