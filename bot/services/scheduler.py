"""Periodic jobs: archive + reminders every 30 s, GitHub polling every N minutes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import Settings
from bot.services.archive import ArchiveService
from bot.services.hack_notifications import HackNotifications
from bot.services.reminders import ReminderService
from bot.services.tasks import TaskService

log = logging.getLogger(__name__)

TICK_SECONDS = 30


def _guard(name: str, func: Callable[[], Awaitable[object]]) -> Callable[[], Awaitable[None]]:
    async def wrapper() -> None:
        try:
            await func()
        except Exception:
            log.exception("Scheduled job %s failed", name)

    return wrapper


def create_scheduler(
    settings: Settings,
    archive: ArchiveService,
    reminders: ReminderService,
    tasks: TaskService,
    hack_notifications: HackNotifications,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def tick() -> None:
        await reminders.tick()
        await archive.archive_due()
        await hack_notifications.flush()

    scheduler.add_job(
        _guard("tick", tick),
        "interval",
        seconds=TICK_SECONDS,
        id="tick",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now().astimezone() + timedelta(seconds=5),
    )
    scheduler.add_job(
        _guard("github_poll", tasks.poll_all),
        "interval",
        minutes=settings.github_poll_interval_minutes,
        id="github_poll",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now().astimezone() + timedelta(seconds=20),
    )
    return scheduler
