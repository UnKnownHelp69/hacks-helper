"""Self-service hackathons: membership authorization, drafts, import and publication."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from contextlib import suppress
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
from bot.services.hack_llm import HackathonLLM, page_context
from bot.services.reminders import chat_zone
from bot.services.users import UserDirectory

IMPORT_TIMEOUT_SECONDS = 120
MAX_IMPORT_DOCUMENTS = 4


class HackathonService:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        directory: UserDirectory,
        github: GitHubClient,
        llm: HackathonLLM | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.directory = directory
        self.github = github
        self.llm = llm or HackathonLLM(
            settings.llm_api_key, settings.llm_model, settings.llm_base_url
        )

    async def close(self) -> None:
        await self.llm.close()

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
        previous = None
        if session.data.get("editing_id"):
            previous = await self.hackathon(int(session.data["editing_id"]), session.user_id)
        if (
            fields.get("repo")
            and self.github.configured
            and (previous is None or previous.repo != fields["repo"])
        ):
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

    async def import_post(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        *,
        explicit: bool = False,
        on_progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> HackSession | None:
        if not explicit and not HACK_RE.search(text) and not URL_RE.search(text):
            return None
        await self.require_access(chat_id, user_id)
        tz = await chat_zone(self.storage, chat_id, self.settings.default_timezone)
        data = extract_post(text, tz)
        if explicit and not data.get("title"):
            title = next(
                (
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and not URL_RE.fullmatch(line.strip())
                ),
                None,
            )
            if title:
                data["title"] = title[:100]
        post_data = dict(data)
        source = data.get("site_url") or next(
            (
                match.group()
                for match in URL_RE.finditer(text)
                if "github.com/" not in match.group()
            ),
            None,
        )
        recognized = explicit or bool(HACK_RE.search(text))
        if source:
            # The read path reports invalid URLs without fetching them.
            with suppress(UserError):
                source = public_url(source)
            data["source_key"] = "url:" + hashlib.sha256(source.encode()).hexdigest()
        else:
            data["source_key"] = "post:" + hashlib.sha256(text.strip().encode()).hexdigest()
        visited: set[str] = set()
        pages: list[dict[str, Any]] = []
        discovered: set[str] = set()
        pending: list[str] = []
        read_attempts = 0

        async def report(stage: str) -> None:
            if on_progress:
                await on_progress(stage, dict(data))

        def warning(message: str) -> None:
            if message not in data.get("import_error", ""):
                data["import_error"] = " ".join(filter(None, [data.get("import_error"), message]))

        def merge(values: dict[str, Any], *, llm: bool = False) -> list[str]:
            nonlocal recognized
            values = dict(values)
            recognized = values.pop("is_hackathon", False) or recognized
            follow = values.pop("follow_urls", [])
            checkpoints = values.pop("checkpoints", [])
            data.update({key: value for key, value in values.items() if value is not None})
            if checkpoints:
                combined = [*data.get("checkpoints", []), *checkpoints]
                unique = {(item["title"].casefold(), item["at"]): item for item in combined}
                data["checkpoints"] = list(unique.values())[:20]
            data.update({key: value for key, value in post_data.items() if value})
            if llm:
                data["llm_imported"] = True
            return follow

        async def read(url: str) -> tuple[str, str]:
            nonlocal read_attempts
            read_attempts += 1
            url = public_url(url)
            visited.add(url)
            html, final_url = await fetch_public_page(url)
            visited.add(final_url)
            context = page_context(html, final_url)
            pages.append(context)
            discovered.update(link["url"] for link in context["links"])
            # Keep the original website and provenance when parsing related pages.
            page = extract_page(html, final_url, tz)
            if len(pages) > 1:
                for key in ("site_url", "source_url", "title"):
                    page.pop(key, None)
                page = {
                    key: value
                    for key, value in page.items()
                    if data.get(key) is None or key in {"checkpoints", "is_hackathon"}
                }
            merge(page)
            return html, final_url

        try:
            async with asyncio.timeout(IMPORT_TIMEOUT_SECONDS):
                await report("Читаю страницу" if source else "Разбираю пост")
                html, final_url = "", None
                if source:
                    try:
                        html, final_url = await read(source)
                        await report("Данные со страницы найдены")
                    except UserError as exc:
                        warning(exc.message)
                if self.llm.configured:
                    # One main page and at most three linked documents; no autonomous URL guesses.
                    for index in range(MAX_IMPORT_DOCUMENTS):
                        await report(
                            "Нейросеть извлекает данные"
                            if index == 0
                            else "Нейросеть уточняет данные по документам"
                        )
                        previous = [
                            {
                                "url": page["url"],
                                "text": page["text"][:12000],
                                "json_ld": page["json_ld"][:4000],
                            }
                            for page in pages[:-1]
                        ]
                        try:
                            result = await self.llm.extract(
                                text, html, final_url, tz, previous_pages=previous, current=data
                            )
                        except UserError as exc:
                            warning(exc.message)
                            break
                        follow = merge(result, llm=True)
                        await report("Найдены новые данные")
                        for candidate in follow:
                            try:
                                candidate = public_url(candidate)
                            except UserError:
                                continue
                            if (
                                candidate in discovered
                                and candidate not in visited
                                and candidate not in pending
                            ):
                                pending.append(candidate)
                        if index == MAX_IMPORT_DOCUMENTS - 1:
                            break
                        html, final_url = "", None
                        while pending and read_attempts < MAX_IMPORT_DOCUMENTS:
                            candidate = pending.pop(0)
                            if candidate in visited:
                                continue
                            await report("Читаю связанный документ: " + candidate)
                            try:
                                html, final_url = await read(candidate)
                                await report("Данные из документа найдены")
                                break
                            except UserError:
                                warning("Не удалось прочитать один из связанных документов.")
                        if final_url is None:
                            break
        except TimeoutError:
            warning("Обработка заняла две минуты и завершена. Уже найденные данные сохранены.")
        if not recognized:
            await report("Хакатон в сообщении не найден")
            return None
        data["imported"] = True
        data["import_sources"] = [page["url"] for page in pages]
        await self.require_access(chat_id, user_id)
        return await self.storage.workflow.new_session(user_id, chat_id, "preview", data)
