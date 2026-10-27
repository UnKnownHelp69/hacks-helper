"""Plain data objects returned by the repositories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

HACK_ACTIVE = "active"
HACK_FINISHED = "finished"

TASK_NOT_STARTED = "not_started"
TASK_IN_PROGRESS = "in_progress"
TASK_CLOSED = "closed"

TASK_SOURCE_BOT = "bot"
TASK_SOURCE_GITHUB = "github"

MILESTONE_REGISTRATION = "registration_end"
MILESTONE_START = "start"
MILESTONE_END = "end"
MILESTONES = (MILESTONE_REGISTRATION, MILESTONE_START, MILESTONE_END)


@dataclass
class UserRecord:
    user_id: int
    username: str | None
    first_name: str
    last_name: str | None
    updated_at: datetime

    @property
    def full_name(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or f"id{self.user_id}"


@dataclass
class Hackathon:
    id: int
    chat_id: int
    slug: str
    title: str | None
    registration_end: datetime
    start_at: datetime
    end_at: datetime
    repo: str | None
    site_url: str | None
    org_chat_url: str | None
    spec_url: str | None
    spec_file_id: str | None
    spec_file_name: str | None
    status: str
    place: int | None
    created_at: datetime
    finished_at: datetime | None
    created_by: int | None = None
    team_min: int = 1
    team_max: int = 4
    source_key: str | None = None
    revision: int = 0

    @property
    def is_active(self) -> bool:
        return self.status == HACK_ACTIVE

    @property
    def display_name(self) -> str:
        return self.title or self.slug

    def milestone_at(self, milestone: str) -> datetime:
        return {
            MILESTONE_REGISTRATION: self.registration_end,
            MILESTONE_START: self.start_at,
            MILESTONE_END: self.end_at,
        }[milestone]


@dataclass
class Task:
    id: int
    hackathon_id: int
    user_id: int
    issue_number: int
    issue_title: str
    issue_url: str
    status: str
    active: bool
    seen_assigned: bool
    source: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


@dataclass
class DeferredPing:
    hackathon_id: int
    milestone: str
    milestone_at: datetime
    user_id: int
    due_at: datetime


@dataclass
class Checkpoint:
    id: int
    hackathon_id: int
    title: str
    at: datetime


@dataclass
class Team:
    id: int
    hackathon_id: int
    name: str
    members: list[int]


@dataclass
class HackSession:
    user_id: int
    chat_id: int
    token: str
    step: str
    data: dict[str, Any]
    expires_at: datetime
    revision: int = 0
