from datetime import UTC, datetime, timedelta

import pytest

from bot.db.models import (
    HACK_ACTIVE,
    HACK_FINISHED,
    MILESTONE_END,
    MILESTONE_REGISTRATION,
    MILESTONE_START,
    TASK_CLOSED,
    TASK_IN_PROGRESS,
    TASK_NOT_STARTED,
    Hackathon,
)
from bot.errors import UserError
from bot.services.schedule_logic import (
    current_phase,
    due_reminder_offsets,
    next_milestone,
    parse_milestone,
    should_archive,
    validate_dates,
)
from bot.services.task_status import determine_status

REG = datetime(2026, 11, 1, 20, 59, tzinfo=UTC)
START = datetime(2026, 11, 5, 7, 0, tzinfo=UTC)
END = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


def hack(status: str = HACK_ACTIVE, **dates: datetime) -> Hackathon:
    return Hackathon(
        id=1,
        chat_id=-1,
        slug="hack",
        title=None,
        registration_end=dates.get("reg", REG),
        start_at=dates.get("start", START),
        end_at=dates.get("end", END),
        repo=None,
        site_url=None,
        org_chat_url=None,
        spec_url=None,
        spec_file_id=None,
        spec_file_name=None,
        status=status,
        place=None,
        created_at=REG,
        finished_at=None,
    )


# --- issue status ---


@pytest.mark.parametrize(
    ("state", "labels", "linked", "expected"),
    [
        ("open", [], False, TASK_NOT_STARTED),
        ("open", ["bug"], False, TASK_NOT_STARTED),
        ("open", ["In Progress"], False, TASK_IN_PROGRESS),  # case-insensitive label
        ("open", [], True, TASK_IN_PROGRESS),  # open linked PR
        ("closed", ["in progress"], True, TASK_CLOSED),
        ("closed", [], False, TASK_CLOSED),
    ],
)
def test_determine_status(state, labels, linked, expected):
    assert determine_status(state, labels, "in progress", linked) == expected


def test_determine_status_custom_label():
    assert determine_status("open", ["в работе"], "в работе", False) == TASK_IN_PROGRESS
    assert determine_status("open", ["in progress"], "в работе", False) == TASK_NOT_STARTED


# --- date validation ---


def test_validate_dates_ok_and_equal_registration_and_start():
    validate_dates(REG, START, END)
    validate_dates(START, START, END)  # registration may end exactly at start


@pytest.mark.parametrize(
    ("reg", "start", "end"),
    [(START + timedelta(minutes=1), START, END), (REG, END, END), (REG, END, START)],
)
def test_validate_dates_rejects_wrong_order(reg, start, end):
    with pytest.raises(UserError):
        validate_dates(reg, start, end)


def test_parse_milestone_aliases():
    assert parse_milestone("рег") == MILESTONE_REGISTRATION
    assert parse_milestone("START") == MILESTONE_START
    assert parse_milestone("конец") == MILESTONE_END
    with pytest.raises(UserError):
        parse_milestone("middle")


# --- /left ---


def test_next_milestone_before_registration_end():
    now = REG - timedelta(days=1, hours=2, minutes=3)
    nxt = next_milestone(hack(), now)
    assert nxt is not None
    assert nxt.milestone == MILESTONE_REGISTRATION
    assert nxt.remaining == timedelta(days=1, hours=2, minutes=3)
    assert current_phase(hack(), now) == "идёт регистрация"


def test_next_milestone_between_registration_and_start():
    nxt = next_milestone(hack(), REG)  # exactly at registration end -> next is start
    assert nxt is not None and nxt.milestone == MILESTONE_START
    assert current_phase(hack(), REG) == "регистрация закрыта, ждём старта"


def test_next_milestone_during_hackathon():
    now = START + timedelta(hours=1)
    nxt = next_milestone(hack(), now)
    assert nxt is not None and nxt.milestone == MILESTONE_END
    assert nxt.remaining == END - now
    assert current_phase(hack(), now) == "хакатон идёт"


def test_next_milestone_after_end():
    assert next_milestone(hack(), END) is None
    assert current_phase(hack(), END + timedelta(days=1)) == "хакатон завершён"


# --- reminders ---

OFFSETS = [24 * 60, 3 * 60, 60]


def test_no_reminder_too_early():
    assert due_reminder_offsets(START, OFFSETS, START - timedelta(hours=25), set()) == []


def test_single_reminder_due():
    now = START - timedelta(hours=23)
    assert due_reminder_offsets(START, OFFSETS, now, set()) == [24 * 60]
    assert due_reminder_offsets(START, OFFSETS, now, {24 * 60}) == []


def test_catch_up_marks_all_due_offsets_at_once():
    # e.g. bot was offline or the date was moved closer
    now = START - timedelta(minutes=30)
    assert due_reminder_offsets(START, OFFSETS, now, {24 * 60}) == [3 * 60, 60]


def test_no_reminder_after_milestone():
    assert due_reminder_offsets(START, OFFSETS, START, set()) == []


# --- archive ---


def test_should_archive():
    assert not should_archive(hack(), END - timedelta(seconds=1))
    assert should_archive(hack(), END)
    assert not should_archive(hack(status=HACK_FINISHED), END + timedelta(days=1))
