"""Automatic move of finished hackathons to the archive."""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from bot.config import Settings
from bot.db.database import utcnow
from bot.db.repositories import Storage
from bot.services.github import GitHubError
from bot.services.schedule_logic import should_archive
from bot.services.tasks import TaskService

log = logging.getLogger(__name__)


class ArchiveService:
    def __init__(self, settings: Settings, storage: Storage, tasks: TaskService) -> None:
        self.settings = settings
        self.storage = storage
        self.tasks = tasks

    async def archive_due(self, now: datetime | None = None) -> list[int]:
        """Archive every active hackathon whose end date has passed. Returns archived ids."""
        now = now or utcnow()
        archived: list[int] = []
        for hack in await self.storage.hackathons.list_all_active():
            if not should_archive(hack, now):
                continue
            allowed = self.settings.is_chat_allowed(hack.chat_id)
            sync_lines: list[str] = []
            if allowed and hack.repo:
                # Last sync so that issues closed right before the end are counted.
                try:
                    sync_lines = await self.tasks.sync_hackathon(hack, now)
                except GitHubError as exc:
                    log.warning("Final sync for %s failed: %s", hack.slug, exc.message)
                except Exception:
                    log.exception("Final sync for %s crashed", hack.slug)
            total, _ = await self.storage.tasks.closed_counts(hack.id)
            text = [
                f"🏁 <b>{escape(hack.display_name)}</b> завершён, закрыто issues: {total}",
            ]
            if sync_lines:
                text = [*sync_lines, "", *text]
            notification = "\n".join(text) if allowed else None
            await self.storage.hackathons.archive(hack.id, now, notification)
            archived.append(hack.id)
            log.info("Hackathon %s (%s) archived", hack.slug, hack.id)
        return archived
