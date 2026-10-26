from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.errors import UserError
from bot.services.availability import (
    MODE_ALL,
    MODE_NONE,
    MODE_WINDOWS,
    DaySchedule,
    WeeklySchedule,
    Window,
    deserialize_schedule,
    is_available,
    next_available,
    parse_day_value,
    parse_days,
    serialize_day,
)

MSK = ZoneInfo("Europe/Moscow")
# 2026-10-26 is a Monday.
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 26, hour, minute, tzinfo=MSK) + timedelta(days=day)


def windows(spec: str) -> DaySchedule:
    return parse_day_value(spec)


@pytest.fixture
def example() -> WeeklySchedule:
    """Example from the spec: Tue OK; Wed 09-21; Thu never; Fri 17-21."""
    return WeeklySchedule(
        {
            TUE: DaySchedule(MODE_ALL),
            WED: windows("09:00-21:00"),
            THU: DaySchedule(MODE_NONE),
            FRI: windows("17:00-21:00"),
        }
    )


def test_default_is_always_available():
    schedule = WeeklySchedule()
    assert schedule.is_default
    assert all(is_available(schedule, at(d, h)) for d in range(7) for h in (0, 12, 23))


def test_spec_example(example):
    assert is_available(example, at(TUE, 3))
    assert not is_available(example, at(WED, 8, 59))
    assert is_available(example, at(WED, 9, 0))
    assert is_available(example, at(WED, 20, 59))
    assert not is_available(example, at(WED, 21, 0))
    assert not is_available(example, at(THU, 12))
    assert not is_available(example, at(FRI, 16, 59))
    assert is_available(example, at(FRI, 17, 0))
    assert is_available(example, at(SAT, 10))  # unset day = allowed


def test_overnight_window_spills_into_next_day():
    schedule = WeeklySchedule({FRI: windows("22:00-02:00"), SAT: DaySchedule(MODE_NONE)})
    assert not is_available(schedule, at(FRI, 21, 59))
    assert is_available(schedule, at(FRI, 22, 0))
    assert is_available(schedule, at(FRI, 23, 59))
    assert is_available(schedule, at(SAT, 0, 30))
    assert is_available(schedule, at(SAT, 1, 59))
    assert not is_available(schedule, at(SAT, 2, 0))
    assert not is_available(schedule, at(SAT, 12))


def test_overnight_window_from_sunday_wraps_to_monday():
    schedule = WeeklySchedule({SUN: windows("23:00-01:00"), MON: DaySchedule(MODE_NONE)})
    assert is_available(schedule, at(MON, 0, 30))  # Monday 26th, window started Sunday
    assert not is_available(schedule, at(MON, 1, 0))


def test_multiple_windows():
    schedule = WeeklySchedule({MON: windows("09:00-12:00, 18:00-24:00")})
    assert is_available(schedule, at(MON, 10))
    assert not is_available(schedule, at(MON, 13))
    assert is_available(schedule, at(MON, 23, 59))
    assert not is_available(schedule, at(MON, 12, 0))


def test_next_available_now_if_available(example):
    now = at(TUE, 15)
    assert next_available(example, now) == now


def test_next_available_later_same_day(example):
    assert next_available(example, at(WED, 7)) == at(WED, 9)


def test_next_available_skips_forbidden_day(example):
    # Wednesday 22:00 -> Thursday is forbidden -> Friday 17:00
    assert next_available(example, at(WED, 22)) == at(FRI, 17)


def test_next_available_full_day_starts_at_midnight():
    schedule = WeeklySchedule({MON: DaySchedule(MODE_NONE)})
    assert next_available(schedule, at(MON, 10)) == at(TUE, 0)


def test_next_available_none_when_never():
    schedule = WeeklySchedule({d: DaySchedule(MODE_NONE) for d in range(7)})
    assert next_available(schedule, at(MON, 10)) is None


def test_parse_days():
    assert parse_days("пн") == [MON]
    assert parse_days("пн-пт") == [0, 1, 2, 3, 4]
    assert parse_days("сб,вс") == [5, 6]
    assert parse_days("сб-вт") == [0, 1, 5, 6]  # wraps around the week
    assert parse_days("будни") == [0, 1, 2, 3, 4]
    assert parse_days("выходные") == [5, 6]
    assert parse_days("все") == list(range(7))
    assert parse_days("Fri") == [FRI]
    for bad in ("пнд", "пн-xx", ""):
        with pytest.raises(UserError):
            parse_days(bad)


def test_parse_day_value():
    assert parse_day_value("да").mode == MODE_ALL
    assert parse_day_value("нет").mode == MODE_NONE
    value = parse_day_value("17:00-21:00")
    assert value.mode == MODE_WINDOWS
    assert value.windows == (Window(17 * 60, 21 * 60),)
    assert parse_day_value("9-21").windows == (Window(9 * 60, 21 * 60),)
    assert parse_day_value("22:00-02:00").windows[0].crosses_midnight
    assert parse_day_value("18:00-24:00").windows == (Window(18 * 60, 0),)
    for bad in ("25:00-26:00", "09:00", "10:00-10:00", "09:60-10:00", "abc"):
        with pytest.raises(UserError):
            parse_day_value(bad)


def test_serialization_roundtrip(example):
    raw = {day: serialize_day(example.day(day)) for day in range(7)}
    restored = deserialize_schedule(raw)
    for day in range(7):
        assert restored.day(day) == example.day(day)
