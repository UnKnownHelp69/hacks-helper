"""Container for long-lived services, injected into handlers as ``ctx``."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from html import escape
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.db.models import Hackathon
from bot.db.repositories import Storage
from bot.errors import UserError
from bot.services.archive import ArchiveService
from bot.services.github import GitHubClient
from bot.services.notify import Messenger, Pinger
from bot.services.reminders import ReminderService, chat_zone
from bot.services.tasks import TaskService
from bot.services.users import UserDirectory


@dataclass
class IssueQuery:
    chat_id: int
    hackathon_id: int
    assignee_login: str | None
    label: str | None


@dataclass
class AppContext:
    settings: Settings
    storage: Storage
    github: GitHubClient
    directory: UserDirectory
    pinger: Pinger
    messenger: Messenger
    tasks: TaskService
    reminders: ReminderService
    archive: ArchiveService
    issue_queries: OrderedDict[str, IssueQuery] = field(default_factory=OrderedDict)

    async def zone(self, chat_id: int) -> ZoneInfo:
        return await chat_zone(self.storage, chat_id, self.settings.default_timezone)

    async def hackathon(self, chat_id: int, slug: str | None) -> Hackathon:
        if not slug:
            raise UserError("Укажите хакатон (короткое имя). Список: /hacks")
        hack = await self.storage.hackathons.get(chat_id, slug.lower())
        if hack is None:
            raise UserError(f"Хакатон «{escape(slug)}» не найден. Список: /hacks, архив: /history")
        return hack

    def remember_issue_query(self, query: IssueQuery) -> str:
        token = secrets.token_urlsafe(6)
        self.issue_queries[token] = query
        while len(self.issue_queries) > 200:
            self.issue_queries.popitem(last=False)
        return token
