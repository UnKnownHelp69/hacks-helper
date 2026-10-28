"""Validation shared by manual forms, imported drafts and edits."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from bot.db.database import to_db, utcnow
from bot.errors import UserError
from bot.services.github import normalize_repo
from bot.services.hack_import import public_url
from bot.services.schedule_logic import date_order_errors, validate_dates
from bot.services.timeutil import parse_datetime_tokens

FIELD_LABELS = {
    "title": "Название",
    "registration_end": "Конец регистрации",
    "start_at": "Начало",
    "end_at": "Конец",
    "team_max": "Максимум в команде",
    "site_url": "Сайт",
    "repo": "Репозиторий GitHub",
    "org_chat_url": "Чат организаторов",
    "spec_url": "ТЗ: ссылка или файл",
}
REQUIRED_FIELDS = ("title", "registration_end", "start_at", "end_at", "team_max")
DATE_FIELDS = {"registration_end", "start_at", "end_at"}


def input_datetime(raw: str, tz: ZoneInfo) -> str:
    return to_db(parse_datetime_tokens(raw.split(), tz))


def input_field(field: str, raw: str, tz: ZoneInfo) -> Any:
    raw = raw.strip()
    if field not in FIELD_LABELS:
        raise UserError("Неизвестное поле")
    if raw in {"-", "нет"} and field not in REQUIRED_FIELDS:
        return None
    if field in DATE_FIELDS:
        return input_datetime(raw, tz)
    if field == "team_max":
        if not raw.isdigit() or not 1 <= int(raw) <= 100:
            raise UserError("Укажите целое число от 1 до 100")
        return int(raw)
    if field == "title":
        if not raw or len(raw) > 100:
            raise UserError("Название должно содержать от 1 до 100 символов")
        return raw
    if field == "repo":
        return normalize_repo(raw)
    return public_url(raw)


def parse_checkpoint(raw: str, tz: ZoneInfo) -> dict[str, str]:
    title, separator, at = raw.partition("|")
    if not separator or not title.strip() or len(title.strip()) > 80:
        raise UserError("Формат: Название чекпоинта | ДД.ММ.ГГГГ ЧЧ:ММ")
    return {"title": title.strip(), "at": input_datetime(at.strip(), tz)}


def _aware(raw: Any) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise UserError("Проверьте даты хакатона") from None
    if value.tzinfo is None:
        raise UserError("У даты должен быть часовой пояс")
    return value


def import_date_issues(data: dict[str, Any]) -> list[str]:
    """Check known dates even in incomplete drafts, using the publication invariants."""
    try:
        dates = {key: _aware(data[key]) if data.get(key) else None for key in DATE_FIELDS}
        return date_order_errors(dates["registration_end"], dates["start_at"], dates["end_at"])
    except UserError as exc:
        return [exc.message]


def validate_hack(data: dict[str, Any]) -> dict[str, Any]:
    missing = [
        FIELD_LABELS[key] for key in REQUIRED_FIELDS if data.get(key) is None or data.get(key) == ""
    ]
    if missing:
        raise UserError("Заполните поля: " + ", ".join(missing))
    title = data["title"]
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise UserError("Название должно содержать от 1 до 100 символов")
    dates = {key: _aware(data[key]) for key in DATE_FIELDS}
    validate_dates(dates["registration_end"], dates["start_at"], dates["end_at"])
    if dates["end_at"] <= utcnow():
        raise UserError("Окончание хакатона должно быть в будущем")
    maximum = data["team_max"]
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        raise UserError("Размер команды должен быть целым числом")
    if not 1 <= maximum <= 100:
        raise UserError("Размер команды должен быть от 1 до 100")
    checkpoints = data.get("checkpoints", [])
    if not isinstance(checkpoints, list) or len(checkpoints) > 20:
        raise UserError("Можно добавить до 20 чекпоинтов")
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("title"), str):
            raise UserError("Проверьте название чекпоинта")
        if not 1 <= len(checkpoint["title"].strip()) <= 80:
            raise UserError("Название чекпоинта: от 1 до 80 символов")
        if _aware(checkpoint.get("at")) > dates["end_at"]:
            raise UserError("Чекпоинт должен наступать до окончания хакатона")
    fields: dict[str, Any] = {
        "title": title.strip(),
        # Keep the legacy database constraint compatible; there is no minimum setting.
        "team_min": 1,
        "team_max": maximum,
        **{key: to_db(value) for key, value in dates.items()},
    }
    for key in ("site_url", "org_chat_url", "spec_url"):
        fields[key] = public_url(data[key]) if data.get(key) else None
    fields["repo"] = normalize_repo(data["repo"]) if data.get("repo") else None
    fields["spec_file_id"] = data.get("spec_file_id")
    fields["spec_file_name"] = data.get("spec_file_name")
    return fields
