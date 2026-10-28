"""Extract editable hackathon data from posts and public web pages."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from contextlib import suppress
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver

from bot.errors import UserError

URL_RE = re.compile(r"https?://[^\s<>\"\[\]]+", re.IGNORECASE)
HACK_RE = re.compile(r"хакатон|hackathon", re.IGNORECASE)
DATE_RE = re.compile(
    r"(?P<day>\d{1,2})[./](?P<month>\d{1,2})[./](?P<year>20\d{2})"
    r"(?:\s*(?:в|,)?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
WORD_DATE_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>" + "|".join(MONTHS) + r")\s+(?P<year>20\d{2})"
    r"(?:\s*(?:в|,)?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?",
    re.IGNORECASE,
)
RANGE_RE = re.compile(
    r"(?P<first>\d{1,2})\s*[-–—]\s*(?P<last>\d{1,2})\s+(?P<month>"
    + "|".join(MONTHS)
    + r")\s+(?P<year>20\d{2})",
    re.IGNORECASE,
)


def public_url(raw: str) -> str:
    """Reject credentials, local literals and non-web protocols before any network call."""
    try:
        parts = urlsplit(raw.strip().rstrip(".,);"))
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise UserError("Некорректная ссылка") from None
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 80, 443}
        or len(raw) > 2000
        or any(ord(char) < 33 for char in raw.strip())
    ):
        raise UserError("Нужна публичная ссылка http:// или https:// без логина и пароля")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.lower().endswith((".local", ".localhost", ".internal")):
            raise UserError("Локальные адреса недоступны") from None
    else:
        if not address.is_global:
            raise UserError("Локальные адреса недоступны")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


class PublicResolver(AbstractResolver):
    """Validate and return the actual socket addresses, preventing DNS rebinding."""

    def __init__(self) -> None:
        self.delegate = ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        addresses = await self.delegate.resolve(host, port, family)
        if not addresses or any(
            not ipaddress.ip_address(item["host"]).is_global for item in addresses
        ):
            raise OSError("Non-public destination")
        return addresses

    async def close(self) -> None:
        await self.delegate.close()


async def fetch_public_page(url: str) -> tuple[str, str]:
    """Read at most 1 MB, validating every redirect and resolved destination."""
    current = public_url(url)
    resolver = PublicResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        async with (
            aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=15),
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                headers={"User-Agent": "hacks-helper/1.0", "Accept-Encoding": "identity"},
            ) as session,
            asyncio.timeout(20),
        ):
            for _ in range(5):
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            break
                        current = public_url(urljoin(current, location))
                        continue
                    if response.status != 200:
                        raise UserError("Сайт не отдал страницу. Можно заполнить данные вручную")
                    if response.content_type not in {
                        "text/html",
                        "text/plain",
                        "application/json",
                        "application/ld+json",
                    }:
                        raise UserError("По ссылке нужна веб-страница с описанием хакатона")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        data.extend(chunk)
                        if len(data) > 1_000_000:
                            raise UserError("Страница слишком большая; заполните данные вручную")
                    return data.decode(response.charset or "utf-8", errors="replace"), current
    except (aiohttp.ClientError, OSError, TimeoutError, LookupError):
        raise UserError("Не удалось прочитать сайт. Можно заполнить данные вручную") from None
    finally:
        await resolver.close()
    raise UserError("Слишком много перенаправлений; заполните данные вручную")


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.metadata: dict[str, str] = {}
        self.structured: list[Any] = []
        self.links: list[dict[str, str]] = []
        self._anchor: tuple[str, int] | None = None
        self._hidden = 0
        self._json = False
        self._script: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href") and not self._hidden:
            self._anchor = (values["href"], len(self.text))
        if tag in {"script", "style", "noscript"}:
            self._hidden += 1
            self._json = tag == "script" and values.get("type") == "application/ld+json"
            self._script = []
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.metadata[key] = values["content"] or ""
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"} and not self._hidden:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor:
            href, start = self._anchor
            self.links.append({"url": href, "label": "".join(self.text[start:]).strip()[:200]})
            self._anchor = None
        if tag in {"script", "style", "noscript"}:
            if self._json:
                with suppress(ValueError, RecursionError):
                    self.structured.append(json.loads("".join(self._script)))
            self._hidden = max(0, self._hidden - 1)
            self._json = False
        elif tag in {"p", "div", "li", "h1", "h2", "h3"} and not self._hidden:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._json:
            self._script.append(data)
        elif not self._hidden:
            self.text.append(data)


def _iso(raw: Any, tz: ZoneInfo) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=tz)).astimezone(UTC).isoformat()


def _line_dates(line: str, tz: ZoneInfo, *, end_of_day: bool = False) -> list[str]:
    if match := RANGE_RE.search(line):
        try:
            month = MONTHS[match["month"].lower()]
            first = datetime(int(match["year"]), month, int(match["first"]), tzinfo=tz)
            last = datetime(int(match["year"]), month, int(match["last"]), 23, 59, tzinfo=tz)
            return [first.astimezone(UTC).isoformat(), last.astimezone(UTC).isoformat()]
        except ValueError:
            return []
    values = []
    for pattern in (DATE_RE, WORD_DATE_RE):
        for match in pattern.finditer(line):
            parts = match.groupdict()
            month = MONTHS.get(parts["month"].lower()) or int(parts["month"])
            try:
                value = datetime(
                    int(parts["year"]),
                    month,
                    int(parts["day"]),
                    int(parts["hour"] or (23 if end_of_day else 0)),
                    int(parts["minute"] or (59 if end_of_day else 0)),
                    tzinfo=tz,
                )
            except ValueError:
                continue
            values.append((match.start(), value.astimezone(UTC).isoformat()))
    return [value for _, value in sorted(values)]


def extract_post(text: str, tz: ZoneInfo) -> dict[str, Any]:
    """Only copy dates and limits actually present; missing fields stay editable and empty."""
    lines = [line.strip() for line in text[:20000].splitlines() if line.strip()]
    result: dict[str, Any] = {"checkpoints": []}
    title = next(
        (line for line in lines if HACK_RE.search(line) and not URL_RE.fullmatch(line)), None
    )
    if title:
        result["title"] = title[:100]
    urls = [match.group().rstrip(".,);") for match in URL_RE.finditer(text)]
    for url in urls:
        if "github.com/" in url:
            match = re.search(r"github\.com/([\w.-]+/[\w.-]+)", url)
            if match:
                result.setdefault("repo", match[1].removesuffix(".git"))
        elif "t.me/" not in url:
            result.setdefault("site_url", url)
    for line in lines:
        lower = line.lower()
        if re.search(r"регистрац|при[её]м заявок|подач[аи] заявок", lower):
            dates = _line_dates(line, tz, end_of_day=True)
            if dates:
                result["registration_end"] = dates[-1]
        elif re.search(r"чекпоинт|checkpoint|защита|презентация", lower):
            dates = _line_dates(line, tz)
            if dates:
                result["checkpoints"].append({"title": line.split(":", 1)[0][:80], "at": dates[0]})
        else:
            dates = _line_dates(
                line, tz, end_of_day=bool(re.search(r"конец|окончани|финиш", lower))
            )
            if len(dates) >= 2:
                result.setdefault("start_at", dates[0])
                result.setdefault("end_at", dates[-1])
            elif dates and re.search(r"начало|старт|пройд[её]т|состоится", lower):
                result["start_at"] = dates[0]
            elif dates and re.search(r"конец|окончани|финиш|завершени", lower):
                result["end_at"] = dates[0]
        if ("команд" in lower or "team" in lower) and not RANGE_RE.search(line):
            limits = re.search(r"(?:от\s*)?(\d{1,2})\s*(?:до|[-–—])\s*(\d{1,2})", lower)
            exact = re.search(r"(?:из|ровно)\s*(\d{1,2})\s*(?:человек|участник)", lower)
            if limits and 1 <= int(limits[1]) <= int(limits[2]) <= 100:
                result.update(team_min=int(limits[1]), team_max=int(limits[2]))
            elif exact and int(exact[1]) > 0:
                result.update(team_min=int(exact[1]), team_max=int(exact[1]))
        if re.search(r"чат|организатор", lower):
            url = next((u for u in urls if "t.me/" in u and u in line), None)
            if url:
                result["org_chat_url"] = url
    return result


def _events(value: Any):
    pending = [value]
    for _ in range(10000):
        if not pending:
            break
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            types = item.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and any(str(kind).endswith("Event") for kind in types):
                yield item
            pending.extend(
                value for value in reversed(list(item.values())) if isinstance(value, (list, dict))
            )


def extract_page(html: str, url: str, tz: ZoneInfo) -> dict[str, Any]:
    parser = PageParser()
    parser.feed(html)
    text = "".join(parser.text)
    result = extract_post(text, tz)
    title = parser.metadata.get("og:title")
    if title:
        result["title"] = title[:100]
    for event in _events(parser.structured):
        if event.get("name"):
            result["title"] = str(event["name"])[:100]
        for target, key in (("start_at", "startDate"), ("end_at", "endDate")):
            if value := _iso(event.get(key), tz):
                result[target] = value
        offers = event.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict) and (value := _iso(offers.get("validThrough"), tz)):
            result["registration_end"] = value
        for target, key in (("team_min", "minTeamSize"), ("team_max", "maxTeamSize")):
            raw = event.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 100:
                result[target] = raw
        break
    result["site_url"] = public_url(url)
    result["source_url"] = result["site_url"]
    result["is_hackathon"] = bool(HACK_RE.search(text + " " + str(result.get("title", ""))))
    return result
