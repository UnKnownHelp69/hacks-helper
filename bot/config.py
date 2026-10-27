"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when the configuration is invalid."""


def _parse_int_list(raw: str | None, name: str) -> list[int]:
    if not raw:
        return []
    result: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name}: '{part}' is not an integer") from exc
    return result


def _parse_positive_int(raw: str | None, name: str, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: '{raw}' is not an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _parse_offsets(raw: str | None) -> list[int]:
    """Parse reminder offsets in hours (floats allowed) into minutes, descending."""
    if raw is None or not raw.strip():
        return [24 * 60, 3 * 60, 60]
    minutes: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hours = float(part)
        except ValueError as exc:
            raise ConfigError(f"REMINDER_OFFSETS_HOURS: '{part}' is not a number") from exc
        if hours <= 0:
            raise ConfigError("REMINDER_OFFSETS_HOURS values must be positive")
        minutes.add(round(hours * 60))
    return sorted(minutes, reverse=True)


@dataclass
class Settings:
    bot_token: str
    allowed_chat_ids: set[int] = field(default_factory=set)
    bot_admin_ids: frozenset[int] = field(default_factory=frozenset)
    github_token: str | None = None
    github_poll_interval_minutes: int = 10
    github_in_progress_label: str = "in progress"
    reminder_offsets_minutes: list[int] = field(default_factory=lambda: [1440, 180, 60])
    default_timezone: str = "Europe/Moscow"
    database_path: Path = Path("data/bot.db")
    log_file: Path | None = Path("logs/bot.log")
    log_level: str = "INFO"
    user_cache_ttl_seconds: int = 300

    @property
    def setup_mode(self) -> bool:
        """True when ALLOWED_CHAT_IDS is empty: /help in groups and /me in private chats."""
        return not self.allowed_chat_ids

    def is_chat_allowed(self, chat_id: int) -> bool:
        return chat_id in self.allowed_chat_ids

    def is_admin_id(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.bot_admin_ids


def load_settings(env_file: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from the environment (and an optional .env file)."""
    load_dotenv(env_file or ".env", override=False)

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is not set (see .env.example)")

    tz_name = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"DEFAULT_TIMEZONE: unknown timezone '{tz_name}'") from exc

    log_file_raw = os.getenv("LOG_FILE", "logs/bot.log").strip()

    return Settings(
        bot_token=token,
        allowed_chat_ids=set(_parse_int_list(os.getenv("ALLOWED_CHAT_IDS"), "ALLOWED_CHAT_IDS")),
        bot_admin_ids=frozenset(_parse_int_list(os.getenv("BOT_ADMIN_IDS"), "BOT_ADMIN_IDS")),
        github_token=os.getenv("GITHUB_TOKEN", "").strip() or None,
        github_poll_interval_minutes=_parse_positive_int(
            os.getenv("GITHUB_POLL_INTERVAL_MINUTES"), "GITHUB_POLL_INTERVAL_MINUTES", 10
        ),
        github_in_progress_label=os.getenv("GITHUB_IN_PROGRESS_LABEL", "in progress").strip()
        or "in progress",
        reminder_offsets_minutes=_parse_offsets(os.getenv("REMINDER_OFFSETS_HOURS")),
        default_timezone=tz_name,
        database_path=Path(os.getenv("DATABASE_PATH", "data/bot.db").strip() or "data/bot.db"),
        log_file=Path(log_file_raw) if log_file_raw else None,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        user_cache_ttl_seconds=_parse_positive_int(
            os.getenv("USER_CACHE_TTL_SECONDS"), "USER_CACHE_TTL_SECONDS", 300
        ),
    )
