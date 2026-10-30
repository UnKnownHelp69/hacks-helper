"""Minimal GitHub REST API client.

Write operations are deliberately limited to: creating an issue and changing
issue assignees. There is no code path that closes, edits or deletes anything.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from html import escape
from typing import Any

import httpx

from bot.errors import UserError

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
_REPO_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})$")
_REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?(?:[#?].*)?$"
)
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_ETAG_CACHE_LIMIT = 2000


class GitHubError(UserError):
    """Any GitHub failure; the message is user-facing."""


class GitHubNotConfigured(GitHubError):
    pass


class GitHubNotFound(GitHubError):
    pass


class GitHubAuthError(GitHubError):
    pass


class GitHubForbidden(GitHubError):
    pass


class GitHubRateLimited(GitHubError):
    def __init__(self, message: str, reset_at: float) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GitHubUnavailable(GitHubError):
    pass


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str
    html_url: str
    assignees: tuple[str, ...]
    labels: tuple[str, ...]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Issue:
        return cls(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            state=str(data.get("state") or "open"),
            html_url=str(data.get("html_url") or ""),
            assignees=tuple(a["login"] for a in data.get("assignees") or [] if a.get("login")),
            labels=tuple(
                (lbl["name"] if isinstance(lbl, dict) else str(lbl))
                for lbl in data.get("labels") or []
            ),
        )


def normalize_repo(raw: str) -> str:
    """Accept 'owner/repo' or a github.com URL and return 'owner/repo'."""
    raw = raw.strip()
    url_match = _REPO_URL_RE.match(raw)
    if url_match:
        raw = f"{url_match.group(1)}/{url_match.group(2)}"
    if not _REPO_RE.match(raw) or raw.rsplit("/", 1)[-1] in {".", ".."}:
        raise UserError(
            f"Неверный репозиторий «{escape(raw)}». Формат: owner/repo или ссылка на github.com"
        )
    return raw


def normalize_login(raw: str) -> str:
    login = raw.strip().lstrip("@")
    if login.lower().startswith("https://github.com/"):
        login = login[len("https://github.com/") :].strip("/")
    if not _LOGIN_RE.match(login):
        raise UserError(f"Неверный GitHub-логин «{escape(raw)}»")
    return login


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1]:
            return section[0].strip()[1:-1]
    return None


class GitHubClient:
    def __init__(
        self,
        token: str | None,
        *,
        base_url: str = API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hacks-helper-telegram-bot",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        self._etags: dict[str, tuple[str, Any, str | None]] = {}
        self.blocked_until: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def close(self) -> None:
        await self._client.aclose()

    # --- low level ---

    def _check_ready(self) -> None:
        if not self.token:
            raise GitHubNotConfigured("GitHub не настроен: не задан GITHUB_TOKEN")
        if self.blocked_until > time.time():
            wait = int(self.blocked_until - time.time()) + 1
            raise GitHubRateLimited(
                f"Превышен лимит запросов к GitHub, попробуйте через {wait} с", self.blocked_until
            )

    def _raise_for_response(self, response: httpx.Response, what: str) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            message = str(response.json().get("message", ""))
        except ValueError:
            message = ""
        remaining = response.headers.get("x-ratelimit-remaining")
        retry_after = response.headers.get("retry-after")
        if status == 429 or (
            status == 403
            and (remaining == "0" or retry_after is not None or "rate limit" in message.lower())
        ):
            if retry_after is not None and retry_after.isdigit():
                reset_at = time.time() + int(retry_after)
            elif response.headers.get("x-ratelimit-reset", "").isdigit():
                reset_at = float(response.headers["x-ratelimit-reset"])
            else:
                reset_at = time.time() + 60
            self.blocked_until = max(self.blocked_until, reset_at)
            log.warning("GitHub rate limit hit, pausing until %s", time.ctime(reset_at))
            wait = max(1, int(reset_at - time.time()))
            raise GitHubRateLimited(
                f"Превышен лимит запросов к GitHub, попробуйте через {wait} с", reset_at
            )
        if status == 401:
            raise GitHubAuthError("GitHub отклонил токен: он недействителен или истёк")
        if status == 403:
            raise GitHubForbidden(f"Нет доступа к {escape(what)}: у токена недостаточно прав")
        if status == 404:
            raise GitHubNotFound(f"{escape(what)}: не найдено или у токена нет доступа")
        if status == 410:
            raise GitHubNotFound(f"{escape(what)}: удалено или отключено")
        if status == 422:
            raise GitHubError(f"GitHub отклонил запрос ({escape(what)}): {escape(message)}")
        if status >= 500:
            raise GitHubUnavailable("GitHub сейчас недоступен, попробуйте позже")
        raise GitHubError(f"Ошибка GitHub {status} ({escape(what)})")

    async def _request(self, method: str, url: str, what: str, **kwargs: Any) -> httpx.Response:
        self._check_ready()
        target = self._client.build_request(method, url).url
        base = self._client.base_url
        if (
            (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port)
            or target.scheme != "https"
            or target.username
            or target.password
        ):
            raise GitHubError("GitHub вернул ссылку за пределами своего API")
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            log.warning("GitHub request failed: %s %s: %s", method, url, type(exc).__name__)
            raise GitHubUnavailable("GitHub сейчас недоступен (ошибка сети)") from exc
        if 300 <= response.status_code < 400 and response.status_code != 304:
            raise GitHubUnavailable("GitHub вернул перенаправление; запрос остановлен")
        if response.status_code != 304:
            self._raise_for_response(response, what)
        return response

    async def _get(
        self, url: str, what: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, str | None]:
        """Conditional GET: reuses cached data on 304 Not Modified. Returns (json, next_url)."""
        request = self._client.build_request("GET", url, params=params)
        key = str(request.url)
        headers = {}
        cached = self._etags.get(key)
        if cached:
            headers["If-None-Match"] = cached[0]
        response = await self._request("GET", url, what, params=params, headers=headers)
        if response.status_code == 304 and cached:
            return cached[1], cached[2]
        data = response.json()
        next_url = _next_link(response.headers.get("link"))
        etag = response.headers.get("etag")
        if etag:
            if len(self._etags) >= _ETAG_CACHE_LIMIT:
                self._etags.clear()
            self._etags[key] = (etag, data, next_url)
        return data, next_url

    async def _get_paginated(
        self, url: str, what: str, params: dict[str, Any], max_pages: int
    ) -> list[Any]:
        items: list[Any] = []
        next_url: str | None = url
        current_params: dict[str, Any] | None = params
        pages = 0
        expected_path = self._client.build_request("GET", url).url.path
        while next_url and pages < max_pages:
            if self._client.build_request("GET", next_url).url.path != expected_path:
                raise GitHubError("GitHub вернул страницу другого ресурса; запрос остановлен")
            data, next_url = await self._get(next_url, what, current_params)
            current_params = None  # the "next" link already contains the query
            items.extend(data)
            pages += 1
        return items

    # --- API ---

    async def get_repo(self, repo: str) -> dict[str, Any]:
        data, _ = await self._get(f"/repos/{repo}", f"репозиторий {repo}")
        return data

    async def user_exists(self, login: str) -> bool:
        try:
            await self._get(f"/users/{login}", f"пользователь GitHub {login}")
        except GitHubNotFound:
            return False
        return True

    async def get_issue(self, repo: str, number: int) -> Issue:
        what = f"issue #{number} в {repo}"
        data, _ = await self._get(f"/repos/{repo}/issues/{number}", what)
        if data.get("pull_request"):
            raise GitHubNotFound(f"#{number} в {escape(repo)} — это pull request, а не issue")
        return Issue.from_json(data)

    async def list_open_issues(
        self,
        repo: str,
        *,
        assignee: str | None = None,
        label: str | None = None,
        max_pages: int = 10,
    ) -> list[Issue]:
        params: dict[str, Any] = {
            "state": "open",
            "per_page": 100,
            "sort": "created",
            "direction": "asc",
        }
        if assignee:
            params["assignee"] = assignee
        if label:
            params["labels"] = label
        items = await self._get_paginated(
            f"/repos/{repo}/issues", f"issues в {repo}", params, max_pages
        )
        return [Issue.from_json(item) for item in items if not item.get("pull_request")]

    async def has_open_linked_pr(self, repo: str, number: int) -> bool:
        """True if an open pull request cross-references the issue (e.g. 'Fixes #12')."""
        events = await self._get_paginated(
            f"/repos/{repo}/issues/{number}/timeline",
            f"история issue #{number} в {repo}",
            {"per_page": 100},
            max_pages=5,
        )
        for event in events:
            if event.get("event") != "cross-referenced":
                continue
            source = (event.get("source") or {}).get("issue") or {}
            if source.get("pull_request") and source.get("state") == "open":
                return True
        return False

    async def create_issue(
        self, repo: str, title: str, body: str | None, assignees: list[str] | None = None
    ) -> Issue:
        payload: dict[str, Any] = {"title": title}
        if body:
            payload["body"] = body
        if assignees:
            payload["assignees"] = assignees
        response = await self._request(
            "POST", f"/repos/{repo}/issues", f"создание issue в {repo}", json=payload
        )
        return Issue.from_json(response.json())

    async def add_assignees(self, repo: str, number: int, logins: list[str]) -> Issue:
        response = await self._request(
            "POST",
            f"/repos/{repo}/issues/{number}/assignees",
            f"назначение исполнителя в #{number}",
            json={"assignees": logins},
        )
        return Issue.from_json(response.json())

    async def remove_assignees(self, repo: str, number: int, logins: list[str]) -> Issue:
        response = await self._request(
            "DELETE",
            f"/repos/{repo}/issues/{number}/assignees",
            f"снятие исполнителя в #{number}",
            json={"assignees": logins},
        )
        return Issue.from_json(response.json())
