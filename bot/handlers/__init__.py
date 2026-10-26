"""Router assembly. The admin router goes first so admin commands are matched there."""

from __future__ import annotations

from aiogram import Router

from bot.handlers import admin, availability, common, events, github, hackathons, tasks


def build_router() -> Router:
    root = Router(name="root")
    root.include_routers(
        events.router,
        admin.router,
        common.router,
        hackathons.router,
        github.router,
        tasks.router,
        availability.router,
    )
    return root
