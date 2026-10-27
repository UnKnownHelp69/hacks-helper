"""Self-service hackathons: membership authorization, drafts, import and publication."""

from __future__ import annotations

import hashlib
from typing import Any

from bot.config import Settings
from bot.db.database import to_db
from bot.db.models import Hackathon, HackSession
from bot.db.repositories import Storage
from bot.errors import UserError
from bot.services.github import GitHubClient
from bot.services.hack_data import validate_hack
from bot.services.hack_import import (
    HACK_RE,
    URL_RE,
    extract_page,
    extract_post,
    fetch_public_page,
    public_url,
)
from bot.services.reminders import chat_zone
from bot.services.users import UserDirectory


class HackathonService:
    def __init__(
        self, settings: Settings, storage: Storage, directory: UserDirectory, github: GitHubClient
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.directory = directory
        self.github = github

    async def require_access(self, chat_id: int, user_id: int) -> None:
        if not self.settings.is_chat_allowed(chat_id):
            raise UserError("Этот чат не подключён к боту")
        if chat_id > 0:
            if chat_id != user_id:
                raise UserError("Это личный чат другого пользователя")
            return
        member = await self.directory.lookup(chat_id, user_id, force=True)
        if member.in_group is not True:
            raise UserError("Не удалось подтвердить, что вы участник этого чата. Попробуйте позже")

    async def hackathon(self, hack_id: int, user_id: int) -> Hackathon:
        hack = await self.storage.hackathons.get_by_id(hack_id)
        if hack is None:
            raise UserError("Хакатон недоступен")
        await self.require_access(hack.chat_id, user_id)
        return hack

    async def session(self, user_id: int, token: str) -> HackSession:
        session = await self.storage.workflow.session(user_id, token)
        if session is None:
            raise UserError("Этот диалог устарел или открыт другим пользователем. Откройте /hack")
        await self.require_access(session.chat_id, user_id)
        return session

    async def edit(self, hack_id: int, user_id: int) -> HackSession:
        hack = await self.hackathon(hack_id, user_id)
        data: dict[str, Any] = {
            key: getattr(hack, key)
            for key in self.storage.workflow.FIELDS
            if key not in {"registration_end", "start_at", "end_at"}
        }
        data.update(
            {key: to_db(getattr(hack, key)) for key in ("registration_end", "start_at", "end_at")}
        )
        data.update(editing_id=hack.id, editing_revision=hack.revision)
        data["checkpoints"] = [
            {"title": checkpoint.title, "at": to_db(checkpoint.at)}
            for checkpoint in await self.storage.workflow.checkpoints(hack.id)
        ]
        return await self.storage.workflow.new_session(user_id, hack.chat_id, "preview", data)

    async def publish(self, session: HackSession) -> int:
        await self.require_access(session.chat_id, session.user_id)
        fields = validate_hack(session.data)
        if fields.get("repo") and self.github.configured:
            await self.github.get_repo(fields["repo"])
        return await self.storage.workflow.publish_draft(session, fields)

    async def vote(self, hack_id: int, user_id: int, choice: str) -> int | None:
        await self.hackathon(hack_id, user_id)
        return await self.storage.workflow.vote(hack_id, user_id, choice)

    async def join_team(
        self, hack_id: int, user_id: int, team_id: int | None, *, overflow: bool = False
    ) -> int:
        await self.hackathon(hack_id, user_id)
        return await self.storage.workflow.join_team(hack_id, user_id, team_id, overflow=overflow)

    async def import_post(self, chat_id: int, user_id: int, text: str) -> HackSession | None:
        if not HACK_RE.search(text) and not URL_RE.search(text):
            return None
        await self.require_access(chat_id, user_id)
        tz = await chat_zone(self.storage, chat_id, self.settings.default_timezone)
        data = extract_post(text, tz)
        source = data.get("site_url")
        if source is None:
            source = next(
                (
                    match.group()
                    for match in URL_RE.finditer(text)
                    if "github.com/" not in match.group()
                ),
                None,
            )
        recognized = bool(HACK_RE.search(text))
        if source:
            try:
                source = public_url(source)
                html, final_url = await fetch_public_page(source)
                page = extract_page(html, final_url, tz)
                recognized = recognized or page.pop("is_hackathon", False)
                # Explicit labels in the shared post override a site's generic metadata.
                checkpoints = data.get("checkpoints") or page.get("checkpoints", [])
                data = {
                    **page,
                    **{key: value for key, value in data.items() if value and key != "checkpoints"},
                }
                data["checkpoints"] = checkpoints
            except UserError as exc:
                data["import_error"] = exc.message
            if source:
                data["source_key"] = "url:" + hashlib.sha256(source.encode()).hexdigest()
        if not recognized:
            return None
        data.setdefault("source_key", "post:" + hashlib.sha256(text.strip().encode()).hexdigest())
        data["imported"] = True
        return await self.storage.workflow.new_session(user_id, chat_id, "preview", data)
