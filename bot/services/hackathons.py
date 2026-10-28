"""Self-service hackathons: membership authorization, creation, import and updates."""

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
from bot.services.hack_data import DATE_FIELDS, import_date_issues, validate_hack
from bot.services.hack_import import (
    HACK_RE,
    URL_RE,
    extract_page,
    extract_post,
    fetch_public_page,
    is_regulation,
    public_url,
    regulation_links,
)
from bot.services.hack_llm import HackathonLLM, page_context
from bot.services.reminders import chat_zone
from bot.services.users import UserDirectory

IMPORT_TIMEOUT_SECONDS = 120
MAX_DISCOVERY_PAGES = 6
MAX_REGULATION_DOCUMENTS = 3


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
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_base_url,
            default_provider=settings.llm_provider,
            gigachat_credentials=settings.gigachat_credentials,
            gigachat_scope=settings.gigachat_scope,
            gigachat_model=settings.gigachat_model,
            gigachat_ca_bundle_file=settings.gigachat_ca_bundle_file,
        )

    async def close(self) -> None:
        await self.llm.close()

    async def llm_selection(self, user_id: int) -> tuple[str, str]:
        return await self.storage.users.llm_preference(user_id) or self.llm.default_selection

    async def select_llm(self, chat_id: int, user_id: int, provider: str, model: str) -> None:
        await self.require_access(chat_id, user_id)
        if model not in await self.llm.available_models(provider):
            raise UserError("Модель недоступна. Заново откройте список моделей")
        await self.storage.users.set_llm_preference(user_id, provider, model)

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

    async def session(self, user_id: int, token: str, *, command: str = "/hack") -> HackSession:
        session = await self.storage.workflow.session(user_id, token)
        if session is None:
            raise UserError(
                f"Этот диалог устарел или открыт другим пользователем. Откройте {command}"
            )
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
        return await self.storage.workflow.new_session(user_id, hack.chat_id, "editing", data)

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
        return await self.storage.workflow.save_hack(session, fields)

    async def vote(self, hack_id: int, user_id: int, choice: str) -> int | None:
        await self.hackathon(hack_id, user_id)
        return await self.storage.workflow.vote(hack_id, user_id, choice)

    async def join_team(
        self, hack_id: int, user_id: int, team_id: int | None, *, overflow: bool = False
    ) -> int:
        await self.hackathon(hack_id, user_id)
        return await self.storage.workflow.join_team(hack_id, user_id, team_id, overflow=overflow)

    async def create(self, chat_id: int, user_id: int, data: dict[str, Any]) -> Hackathon:
        await self.require_access(chat_id, user_id)
        validate_hack(data)
        session = await self.storage.workflow.new_session(user_id, chat_id, "saving", data)
        try:
            hack_id = await self.publish(session)
            return await self.hackathon(hack_id, user_id)
        finally:
            await self.storage.workflow.delete_session(session)

    async def import_post(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        *,
        explicit: bool = False,
        on_progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Hackathon | None:
        data = await self.extract_import(
            chat_id, user_id, text, explicit=explicit, on_progress=on_progress
        )
        if data is None:
            return None
        try:
            validate_hack(data)
            if data.get("llm_imported") and not data.get("dates_reviewed"):
                raise UserError("Не удалось перепроверить даты из документов")
        except UserError as exc:
            details = data.get("import_error")
            raise UserError(
                "Хакатон не создан. "
                + exc.message.replace("Заполните поля:", "Не найдены данные:")
                + ("\n" + details if details else "")
            ) from None
        return await self.create(chat_id, user_id, data)

    async def extract_import(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        *,
        explicit: bool = False,
        on_progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        if not explicit and not HACK_RE.search(text) and not URL_RE.search(text):
            return None
        await self.require_access(chat_id, user_id)
        selection = await self.llm_selection(user_id)
        tz = await chat_zone(self.storage, chat_id, self.settings.default_timezone)
        post = extract_post(text, tz)
        source = post.get("site_url") or next(
            (
                match.group()
                for match in URL_RE.finditer(text)
                if "github.com/" not in match.group()
            ),
            None,
        )
        # With a URL, advertising text is not an event-data source. A text-only post is
        # still manually supplied event data, validated before creation.
        data: dict[str, Any] = {"checkpoints": []} if source else post
        recognized = explicit or bool(HACK_RE.search(text))
        if explicit and not source and not data.get("title"):
            title = next((line.strip() for line in text.splitlines() if line.strip()), None)
            if title:
                data["title"] = title[:100]
        if source:
            with suppress(UserError):
                source = public_url(source)
            data["site_url"] = source
            data["source_key"] = "url:" + hashlib.sha256(source.encode()).hexdigest()
        else:
            data["source_key"] = "post:" + hashlib.sha256(text.strip().encode()).hexdigest()
        pages: list[dict[str, Any]] = []  # Official documents only; no landing pages or viewers.
        documents: list[tuple[str, str]] = []
        visited: set[str] = set()

        async def report(stage: str) -> None:
            if on_progress:
                await on_progress(stage, dict(data))

        def warning(message: str) -> None:
            if message not in data.get("import_error", ""):
                data["import_error"] = " ".join(filter(None, [data.get("import_error"), message]))

        def source_context(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "url": page["url"],
                    "text": page["text"],
                    "json_ld": page["json_ld"][:4000],
                }
                for page in items
            ]

        def merge(values: dict[str, Any], *, llm: bool = False) -> None:
            nonlocal recognized
            values = dict(values)
            recognized = values.pop("is_hackathon", False) or recognized
            checkpoints = values.pop("checkpoints", [])
            # The source website and selected regulations are controlled by discovery.
            for key in ("follow_urls", "site_url", "source_url", "spec_url"):
                values.pop(key, None)
            data.update({key: value for key, value in values.items() if value is not None})
            if checkpoints:
                combined = [*data.get("checkpoints", []), *checkpoints]
                unique = {(item["title"].casefold(), item["at"]): item for item in combined}
                data["checkpoints"] = list(unique.values())[:20]
            if llm:
                data["llm_imported"] = True

        async def audit_dates() -> None:
            if not any(data.get(key) for key in DATE_FIELDS):
                return
            issues = import_date_issues(data) or [
                "Подтвердите каждую дату точной цитатой из источника. "
                "Проверьте назначение дедлайна и не добавляйте время, "
                "которого нет в тексте."
            ]
            await report("Нейросеть перепроверяет даты по исходным материалам")
            try:
                reviewed = await self.llm.extract(
                    "",
                    "",
                    None,
                    tz,
                    previous_pages=source_context(pages),
                    current=data,
                    selection=selection,
                    review_issues=issues,
                )
            except UserError as exc:
                warning("Не удалось перепроверить даты. " + exc.message)
            else:
                unconfirmed = any(
                    data.get(key) is not None and reviewed.get(key) is None
                    for key in DATE_FIELDS
                    if key in reviewed
                )
                # Only audit date fields. A null retracts an unsupported date;
                # unrelated facts and manually entered forms are not rewritten.
                data.update({key: reviewed[key] for key in DATE_FIELDS if key in reviewed})
                data["dates_reviewed"] = True
                if unconfirmed:
                    warning("LLM не подтвердила часть дат по источникам. Уточните их вручную.")
                if import_date_issues(data):
                    warning(
                        "После перепроверки даты всё ещё противоречат друг другу. "
                        "Проверьте их вручную."
                    )
                await report("Перепроверка дат завершена")

        try:
            async with asyncio.timeout(IMPORT_TIMEOUT_SECONDS):
                await report("Ищу положение и регламент на сайте" if source else "Разбираю пост")
                pending = [source] if source else []
                attempts = 0
                while (
                    pending
                    and attempts < MAX_DISCOVERY_PAGES
                    and len(documents) < MAX_REGULATION_DOCUMENTS
                ):
                    candidate = pending.pop(0)
                    if candidate in visited:
                        continue
                    visited.add(candidate)
                    attempts += 1
                    await report("Ищу документ: " + candidate)
                    try:
                        html, final_url = await fetch_public_page(candidate)
                    except UserError as exc:
                        warning(exc.message)
                        continue
                    if final_url != candidate and final_url in visited:
                        continue
                    visited.add(final_url)
                    context = page_context(html, final_url)
                    recognized = recognized or bool(
                        HACK_RE.search(
                            context["text"] + " " + str(context["metadata"].get("og:title", ""))
                        )
                    )
                    if is_regulation(html, final_url):
                        pages.append(context)
                        documents.append((html, final_url))
                        data.setdefault("spec_url", final_url)
                        merge(extract_page(html, final_url, tz))
                        await report("Положение / регламент прочитан")
                    links = regulation_links(context["links"])
                    if links:
                        data.setdefault("document_links", links[:3])
                    # Search a linked document/viewer's download before unrelated document hubs.
                    pending = [url for url in links if url not in visited] + pending
                if not documents:
                    warning(
                        "Не удалось найти или прочитать положение / регламент хакатона. "
                        "Пришлите прямую ссылку на документ через импорт "
                        "или заполните поля вручную."
                    )
                elif self.llm.is_configured(selection[0]):
                    data["llm_provider"], data["llm_model"] = selection
                    await report("Нейросеть извлекает данные из положения / регламента")
                    html, document_url = documents[0]
                    try:
                        result = await self.llm.extract(
                            "",
                            html,
                            document_url,
                            tz,
                            previous_pages=source_context(pages[1:]),
                            current=data,
                            selection=selection,
                        )
                    except UserError as exc:
                        warning(exc.message)
                    else:
                        merge(result, llm=True)
                        await report("Данные из документов найдены")
                        await audit_dates()
        except TimeoutError:
            warning("Обработка заняла две минуты и завершена.")
        if not recognized:
            await report("Хакатон в сообщении не найден")
            return None
        data["imported"] = True
        data["import_sources"] = [page["url"] for page in pages]
        data["regulations_import"] = bool(documents)
        await self.require_access(chat_id, user_id)
        return data
