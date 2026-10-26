from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.errors import UserError
from bot.services.timeutil import (
    format_dt,
    format_duration,
    get_zone,
    parse_datetime,
    utc_offset_label,
)

MSK = ZoneInfo("Europe/Moscow")
EKB = ZoneInfo("Asia/Yekaterinburg")


def test_parse_datetime_converts_chat_time_to_utc():
    dt = parse_datetime("01.11.2026", "18:00", MSK)
    assert dt == datetime(2026, 11, 1, 15, 0, tzinfo=UTC)
    assert dt.tzinfo is UTC


def test_parse_datetime_respects_timezone():
    assert parse_datetime("01.11.2026", "18:00", EKB) == datetime(2026, 11, 1, 13, 0, tzinfo=UTC)


def test_parse_datetime_accepts_single_digit_parts():
    assert parse_datetime("1.2.2027", "9:05", MSK) == datetime(2027, 2, 1, 6, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("date_part", "time_part"),
    [
        ("2026-11-01", "18:00"),
        ("01.11.26", "18:00"),
        ("01.11.2026", "18-00"),
        ("01.11.2026", "1800"),
        ("завтра", "18:00"),
    ],
)
def test_parse_datetime_rejects_bad_format(date_part, time_part):
    with pytest.raises(UserError, match=r"ДД\.ММ\.ГГГГ ЧЧ:ММ"):
        parse_datetime(date_part, time_part, MSK)


@pytest.mark.parametrize(
    ("date_part", "time_part"), [("31.02.2026", "10:00"), ("01.11.2026", "25:00")]
)
def test_parse_datetime_rejects_impossible_dates(date_part, time_part):
    with pytest.raises(UserError, match="не существует"):
        parse_datetime(date_part, time_part, MSK)


def test_format_dt_roundtrip():
    dt = parse_datetime("05.11.2026", "10:00", EKB)
    assert format_dt(dt, EKB) == "05.11.2026 10:00"
    assert format_dt(dt, MSK) == "05.11.2026 08:00"


def test_get_zone_valid_and_invalid():
    assert get_zone("Asia/Yekaterinburg").key == "Asia/Yekaterinburg"
    for bad in ("Mars/Olympus", "", "../etc/passwd", "<b>"):
        with pytest.raises(UserError):
            get_zone(bad)


def test_format_duration():
    assert format_duration(timedelta(days=2, hours=3, minutes=15, seconds=59)) == "2 д 3 ч 15 мин"
    assert format_duration(timedelta(hours=1)) == "1 ч"
    assert format_duration(timedelta(seconds=30)) == "0 мин"
    assert format_duration(timedelta(seconds=-5)) == "0 мин"


def test_utc_offset_label():
    now = datetime(2026, 10, 26, tzinfo=UTC)
    assert utc_offset_label(MSK, now) == "UTC+03:00"
    assert utc_offset_label(ZoneInfo("America/St_Johns"), now) == "UTC-02:30"
