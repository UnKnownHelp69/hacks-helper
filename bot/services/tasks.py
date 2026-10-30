"""Tasks (issue -> participant): assignment, GitHub sync and status tracking."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from bot.config import Settings
from bot.db.database import utcnow
from bot.db.models import (
    TASK_CLOSED,
    TASK_SOURCE_BOT,
    TASK_SOURCE_GITHUB,
    Hackathon,
    Task,
)
from bot.db.repositories import Storage
from bot.errors import UserError
from bot.services.formatting import issue_link
from bot.services.github import (
    GitHubClient,
    GitHubError,
    GitHubNotFound,
    GitHubRateLimited,
    Issue,
)
from bot.services.notify import Messenger, Pinger
from bot.services.reminders import chat_zone
from bot.services.task_status import STATUS_TITLES, determine_status, has_label

log = logging.getLogger(__name__)


@dataclass
class TaskResult:
    task: Task | None
    issue: Issue
    warnings: list[str] = field(default_factory=list)


def require_repo(hack: Hackathon) -> str:
    if not hack.repo:
        raise UserError("Добавьте репозиторий в /hack → Редактировать → Репозиторий GitHub")
    return hack.repo


class TaskService:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        github: GitHubClient,
        pinger: Pinger,
        messenger: Messenger,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.github = github
        self.pinger = pinger
        self.messenger = messenger
        self._assignment_lock = asyncio.Lock()

    async def issue_status(self, repo: str, issue: Issue) -> str:
        if issue.state == "closed":
            return TASK_CLOSED
        label = self.settings.github_in_progress_label
        if has_label(issue.labels, label):
            return determine_status(issue.state, issue.labels, label, False)
        try:
            linked = await self.github.has_open_linked_pr(repo, issue.number)
        except GitHubRateLimited:
            raise
        except GitHubError as exc:
            log.warning("Timeline for %s#%s unavailable: %s", repo, issue.number, exc.message)
            linked = False
        return determine_status(issue.state, issue.labels, label, linked)

    async def _ensure_github_assignee(
        self, repo: str, issue: Issue, login: str | None, warnings: list[str]
    ) -> bool:
        """Set the GitHub assignee if a login is linked. Returns True if assigned."""
        if not login:
            warnings.append(
                "GitHub не привязан: задача сохранена в боте. Привязка аккаунта — в /me"
            )
            return False
        if login.lower() in {a.lower() for a in issue.assignees}:
            return True
        try:
            updated = await self.github.add_assignees(repo, issue.number, [login])
        except GitHubError as exc:
            warnings.append(f"assignee не поставлен: {exc.message}")
            return False
        if login.lower() not in {a.lower() for a in updated.assignees}:
            warnings.append(f"GitHub не принял {escape(login)}: нет доступа к репо?")
            return False
        return True

    async def assign(self, hack: Hackathon, user_id: int, issue_number: int) -> TaskResult:
        async with self._assignment_lock:
            current = await self.storage.hackathons.get_by_id(hack.id)
            if current is None:
                raise UserError("Хакатон больше недоступен")
            if current.repo != hack.repo:
                raise UserError("Репозиторий изменился. Откройте список задач заново")
            hack = current
            if not self.settings.is_chat_allowed(hack.chat_id):
                raise UserError("Этот чат не подключён к боту")
            repo = require_repo(hack)
            if not hack.is_active:
                raise UserError("Хакатон в архиве")
            if not await self.storage.hackathons.is_member(hack.id, user_id):
                raise UserError(f"Исполнитель не участвует в {escape(hack.slug)}")
            existing = await self.storage.tasks.get(hack.id, user_id, issue_number)
            if existing and existing.active:
                raise UserError(f"#{issue_number} уже назначена")
            try:
                issue = await self.github.get_issue(repo, issue_number)
            except GitHubNotFound:
                raise UserError(f"#{issue_number} не найден в {escape(repo)}") from None
            if issue.state == "closed":
                raise UserError(f"#{issue_number} уже закрыт")
            warnings: list[str] = []
            login = await self.storage.github_links.get_login(user_id)
            status = await self.issue_status(repo, issue)
            assigned = await self._ensure_github_assignee(repo, issue, login, warnings)
            task = await self.storage.tasks.upsert_active(
                hack.id,
                user_id,
                issue.number,
                issue.title,
                issue.html_url,
                status,
                TASK_SOURCE_BOT,
                assigned,
            )
            delivered = await self.messenger.send(
                hack.chat_id,
                f"📌 <b>Вам назначена задача</b> · {escape(hack.display_name)}\n"
                f"{issue_link(issue.number, issue.title, issue.html_url)}\n"
                f"Статус: {STATUS_TITLES[status]}\nМои задачи: /me → Мои задачи",
                receiver_user_id=user_id,
            )
            if not delivered:
                warnings.append(
                    "Личное уведомление не доставлено. Задача доступна исполнителю в /me"
                )
            return TaskResult(task, issue, warnings)

    async def unassign(self, hack: Hackathon, user_id: int, issue_number: int) -> list[str]:
        if not self.settings.is_chat_allowed(hack.chat_id):
            raise UserError("Этот чат не подключён к боту")
        task = await self.storage.tasks.get(hack.id, user_id, issue_number)
        if task is None or not task.active:
            raise UserError(f"Задачи #{issue_number} нет")
        await self.storage.tasks.delete(task.id)
        warnings: list[str] = []
        login = await self.storage.github_links.get_login(user_id)
        if login and hack.repo and self.github.configured:
            try:
                await self.github.remove_assignees(hack.repo, issue_number, [login])
            except GitHubError as exc:
                warnings.append(f"assignee не снят: {exc.message}")
        return warnings

    async def create_issue(
        self, hack: Hackathon, title: str, body: str | None, assignee_id: int | None
    ) -> TaskResult:
        if not self.settings.is_chat_allowed(hack.chat_id):
            raise UserError("Этот чат не подключён к боту")
        repo = require_repo(hack)
        if not hack.is_active:
            raise UserError("Хакатон в архиве")
        if assignee_id is not None and not await self.storage.hackathons.is_member(
            hack.id, assignee_id
        ):
            raise UserError(f"Исполнитель не участвует в {escape(hack.slug)}")
        issue = await self.github.create_issue(repo, title, body)
        if assignee_id is None:
            return TaskResult(None, issue)
        warnings: list[str] = []
        login = await self.storage.github_links.get_login(assignee_id)
        assigned = await self._ensure_github_assignee(repo, issue, login, warnings)
        task = await self.storage.tasks.upsert_active(
            hack.id,
            assignee_id,
            issue.number,
            issue.title,
            issue.html_url,
            determine_status(
                issue.state, issue.labels, self.settings.github_in_progress_label, False
            ),
            TASK_SOURCE_BOT,
            assigned,
        )
        return TaskResult(task, issue, warnings)

    # --- periodic sync ---

    async def sync_hackathon(self, hack: Hackathon, now: datetime | None = None) -> list[str]:
        """Sync tasks of one hackathon with GitHub. Returns notification lines."""
        if (
            not self.settings.is_chat_allowed(hack.chat_id)
            or not hack.repo
            or not self.github.configured
        ):
            return []
        now = now or utcnow()
        repo = hack.repo
        tz = await chat_zone(self.storage, hack.chat_id, self.settings.default_timezone)
        members = await self.storage.hackathons.members(hack.id)
        links = await self.storage.github_links.logins_for(members)
        login_to_user = {login.lower(): uid for uid, login in links.items()}

        open_issues = await self.github.list_open_issues(repo)
        open_by_number = {issue.number: issue for issue in open_issues}
        tasks = await self.storage.tasks.active_for_hackathon(hack.id)
        existing = {(t.user_id, t.issue_number) for t in tasks}
        lines: list[str] = []

        async def who(user_id: int) -> str:
            return await self.pinger.render_user(hack.chat_id, user_id, tz, now)

        # 1. Assignees set in GitHub -> new tasks for participants with a linked login.
        for issue in open_issues:
            for login in issue.assignees:
                user_id = login_to_user.get(login.lower())
                if user_id is None or (user_id, issue.number) in existing:
                    continue
                status = await self.issue_status(repo, issue)
                await self.storage.tasks.upsert_active(
                    hack.id,
                    user_id,
                    issue.number,
                    issue.title,
                    issue.html_url,
                    status,
                    TASK_SOURCE_GITHUB,
                    True,
                )
                existing.add((user_id, issue.number))
                lines.append(
                    f"📌 {issue_link(issue.number, issue.title, issue.html_url)}: "
                    f"назначено на {await who(user_id)} (из GitHub), {STATUS_TITLES[status]}"
                )

        # 2. Existing tasks: closed issues, removed assignees, status changes.
        for task in tasks:
            issue = open_by_number.get(task.issue_number)
            if issue is None:
                try:
                    issue = await self.github.get_issue(repo, task.issue_number)
                except GitHubNotFound:
                    await self.storage.tasks.delete(task.id)
                    lines.append(
                        f"❔ #{task.issue_number} не найден в репо, снята с "
                        f"{await self.pinger.directory.plain(hack.chat_id, task.user_id)}"
                    )
                    continue
            link = issue_link(issue.number, issue.title, issue.html_url)
            login = links.get(task.user_id)
            if issue.state == "open" and login:
                assigned = login.lower() in {a.lower() for a in issue.assignees}
                if assigned and not task.seen_assigned:
                    await self.storage.tasks.mark_seen_assigned(task.id)
                elif not assigned and task.seen_assigned:
                    await self.storage.tasks.delete(task.id)
                    name = await self.pinger.directory.plain(hack.chat_id, task.user_id)
                    lines.append(f"↩️ {link}: снята с {name} (assignee снят в GitHub)")
                    continue
            status = await self.issue_status(repo, issue)
            if status != task.status:
                await self.storage.tasks.update_status(task.id, status, title=issue.title)
                if status == TASK_CLOSED:
                    lines.append(f"{link} закрыт ✅ ({await who(task.user_id)})")
                else:
                    lines.append(f"🔄 {link}: {await who(task.user_id)} → {STATUS_TITLES[status]}")
            elif issue.title != task.issue_title:
                await self.storage.tasks.update_status(task.id, status, title=issue.title)
        return lines

    async def poll_all(self) -> None:
        """Periodic job: sync every active hackathon in allowed chats."""
        if not self.github.configured:
            return
        for hack in await self.storage.hackathons.list_all_active():
            if not hack.repo or not self.settings.is_chat_allowed(hack.chat_id):
                continue
            try:
                lines = await self.sync_hackathon(hack)
            except GitHubRateLimited:
                log.warning("GitHub rate limit reached, poll postponed")
                return
            except GitHubError as exc:
                log.warning("GitHub sync failed for %s (%s): %s", hack.slug, hack.repo, exc.message)
                continue
            except Exception:
                log.exception("GitHub sync crashed for %s", hack.slug)
                continue
            if lines:
                await self.messenger.send(
                    hack.chat_id, f"🔔 <b>{escape(hack.slug)}</b>\n" + "\n".join(lines)
                )
