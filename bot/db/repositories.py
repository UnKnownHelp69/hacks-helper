"""Data access layer. Users are always referenced by Telegram user_id."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import aiosqlite

from bot.db.database import Database, from_db, to_db, utcnow
from bot.db.hack_workflow import HackWorkflowRepo
from bot.db.models import (
    HACK_ACTIVE,
    HACK_FINISHED,
    TASK_CLOSED,
    DeferredPing,
    Hackathon,
    Task,
    UserRecord,
)

log = logging.getLogger(__name__)


def _user(row: aiosqlite.Row) -> UserRecord:
    return UserRecord(
        user_id=row["user_id"],
        username=row["username"],
        first_name=row["first_name"] or "",
        last_name=row["last_name"],
        updated_at=from_db(row["updated_at"]),
    )


def _hackathon(row: aiosqlite.Row) -> Hackathon:
    return Hackathon(
        id=row["id"],
        chat_id=row["chat_id"],
        slug=row["slug"],
        title=row["title"],
        registration_end=from_db(row["registration_end"]),
        start_at=from_db(row["start_at"]),
        end_at=from_db(row["end_at"]),
        repo=row["repo"],
        site_url=row["site_url"],
        org_chat_url=row["org_chat_url"],
        spec_url=row["spec_url"],
        spec_file_id=row["spec_file_id"],
        spec_file_name=row["spec_file_name"],
        status=row["status"],
        place=row["place"],
        created_at=from_db(row["created_at"]),
        finished_at=from_db(row["finished_at"]),
        created_by=row["created_by"],
        team_min=row["team_min"],
        team_max=row["team_max"],
        source_key=row["source_key"],
        revision=row["revision"],
    )


def _task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        hackathon_id=row["hackathon_id"],
        user_id=row["user_id"],
        issue_number=row["issue_number"],
        issue_title=row["issue_title"],
        issue_url=row["issue_url"],
        status=row["status"],
        active=bool(row["active"]),
        seen_assigned=bool(row["seen_assigned"]),
        source=row["source"],
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
        closed_at=from_db(row["closed_at"]),
    )


class UsersRepo:
    """Registry of every Telegram user the bot has seen."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, user_id: int) -> UserRecord | None:
        row = await self.db.fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return _user(row) if row else None

    async def find_by_username(self, username: str) -> UserRecord | None:
        username = username.lstrip("@")
        row = await self.db.fetchone(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE ORDER BY updated_at DESC",
            (username,),
        )
        return _user(row) if row else None

    async def upsert(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> bool:
        """Insert or refresh a user. Returns True if anything changed.

        If the username now belongs to this user, it is removed from any other
        user_id that still had it in the registry (usernames can be reassigned).
        """
        username = username or None
        first_name = first_name or ""
        existing = await self.get(user_id)
        if (
            existing is not None
            and existing.username == username
            and existing.first_name == first_name
            and existing.last_name == last_name
        ):
            return False

        statements = []
        if username:
            stale = await self.db.fetchall(
                "SELECT user_id FROM users WHERE username = ? COLLATE NOCASE AND user_id != ?",
                (username, user_id),
            )
            for row in stale:
                log.info(
                    "Username @%s moved from user %s to user %s",
                    username,
                    row["user_id"],
                    user_id,
                )
                statements.append(
                    ("UPDATE users SET username = NULL WHERE user_id = ?", (row["user_id"],))
                )
        if existing is not None and existing.username != username:
            log.info("User %s changed username: @%s -> @%s", user_id, existing.username, username)
        statements.append(
            (
                """
                INSERT INTO users (user_id, username, first_name, last_name, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, username, first_name, last_name, to_db(utcnow())),
            )
        )
        await self.db.run_in_transaction(statements)
        return True

    async def clear_username(self, user_id: int) -> None:
        await self.db.execute("UPDATE users SET username = NULL WHERE user_id = ?", (user_id,))

    async def llm_preference(self, user_id: int) -> tuple[str, str] | None:
        row = await self.db.fetchone(
            "SELECT provider, model FROM llm_preferences WHERE user_id=?", (user_id,)
        )
        return (row["provider"], row["model"]) if row else None

    async def set_llm_preference(self, user_id: int, provider: str, model: str) -> None:
        await self.db.execute(
            "INSERT INTO llm_preferences(user_id,provider,model) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET provider=excluded.provider,model=excluded.model",
            (user_id, provider, model),
        )

    async def reset_llm_preference(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM llm_preferences WHERE user_id=?", (user_id,))


class ChatsRepo:
    def __init__(self, db: Database, default_timezone: str) -> None:
        self.db = db
        self.default_timezone = default_timezone

    async def get_timezone(self, chat_id: int) -> str:
        row = await self.db.fetchone("SELECT timezone FROM chats WHERE chat_id = ?", (chat_id,))
        return row["timezone"] if row else self.default_timezone

    async def set_timezone(self, chat_id: int, tz_name: str) -> None:
        await self.db.execute(
            """
            INSERT INTO chats (chat_id, timezone) VALUES (?, ?)
            ON CONFLICT (chat_id) DO UPDATE SET timezone = excluded.timezone
            """,
            (chat_id, tz_name),
        )

    async def migrate_chat(self, old_chat_id: int, new_chat_id: int) -> None:
        """Move every chat-bound record to the new chat id (group -> supergroup)."""
        await self.db.run_in_transaction(
            [
                (
                    "INSERT OR IGNORE INTO chats (chat_id, timezone) "
                    "SELECT ?, timezone FROM chats WHERE chat_id = ?",
                    (new_chat_id, old_chat_id),
                ),
                ("DELETE FROM chats WHERE chat_id = ?", (old_chat_id,)),
                (
                    "UPDATE OR IGNORE hackathons SET chat_id = ? WHERE chat_id = ?",
                    (new_chat_id, old_chat_id),
                ),
                (
                    "UPDATE popups SET chat_id = ? WHERE chat_id = ?",
                    (new_chat_id, old_chat_id),
                ),
                (
                    "UPDATE hack_sessions SET chat_id = ? WHERE chat_id = ?",
                    (new_chat_id, old_chat_id),
                ),
                (
                    """
                    INSERT INTO chat_migrations (old_chat_id, new_chat_id, migrated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (old_chat_id) DO UPDATE SET new_chat_id = excluded.new_chat_id
                    """,
                    (old_chat_id, new_chat_id, to_db(utcnow())),
                ),
            ]
        )

    async def get_migrations(self) -> dict[int, int]:
        rows = await self.db.fetchall("SELECT old_chat_id, new_chat_id FROM chat_migrations")
        return {row["old_chat_id"]: row["new_chat_id"] for row in rows}


class HackathonsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        chat_id: int,
        slug: str,
        title: str | None,
        registration_end: datetime,
        start_at: datetime,
        end_at: datetime,
    ) -> Hackathon:
        hack_id = await self.db.execute(
            """
            INSERT INTO hackathons
                (chat_id, slug, title, registration_end, start_at, end_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                slug,
                title,
                to_db(registration_end),
                to_db(start_at),
                to_db(end_at),
                HACK_ACTIVE,
                to_db(utcnow()),
            ),
        )
        created = await self.get_by_id(hack_id)
        assert created is not None
        return created

    async def get_by_id(self, hack_id: int) -> Hackathon | None:
        row = await self.db.fetchone("SELECT * FROM hackathons WHERE id = ?", (hack_id,))
        return _hackathon(row) if row else None

    async def get(self, chat_id: int, slug: str) -> Hackathon | None:
        row = await self.db.fetchone(
            "SELECT * FROM hackathons WHERE chat_id = ? AND slug = ? COLLATE NOCASE",
            (chat_id, slug),
        )
        return _hackathon(row) if row else None

    async def list(self, chat_id: int, status: str | None = None) -> list[Hackathon]:
        if status is None:
            rows = await self.db.fetchall(
                "SELECT * FROM hackathons WHERE chat_id = ? ORDER BY start_at", (chat_id,)
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM hackathons WHERE chat_id = ? AND status = ? ORDER BY start_at",
                (chat_id, status),
            )
        return [_hackathon(r) for r in rows]

    async def list_all_active(self) -> list[Hackathon]:
        rows = await self.db.fetchall(
            "SELECT * FROM hackathons WHERE status = ? ORDER BY start_at", (HACK_ACTIVE,)
        )
        return [_hackathon(r) for r in rows]

    async def list_active_for_user(self, chat_id: int, user_id: int) -> list[Hackathon]:
        now = to_db(utcnow())
        rows = await self.db.fetchall(
            "SELECT h.* FROM hackathons h "
            "JOIN hackathon_members m ON m.hackathon_id=h.id "
            "WHERE h.chat_id=? AND m.user_id=? AND h.status=? "
            "AND h.end_at>? ORDER BY h.start_at,h.id",
            (chat_id, user_id, HACK_ACTIVE, now),
        )
        return [_hackathon(row) for row in rows]

    async def update_dates(
        self, hack_id: int, registration_end: datetime, start_at: datetime, end_at: datetime
    ) -> None:
        await self.db.execute(
            "UPDATE hackathons SET registration_end = ?, start_at = ?, end_at = ? WHERE id = ?",
            (to_db(registration_end), to_db(start_at), to_db(end_at), hack_id),
        )

    _UPDATABLE = frozenset(
        {
            "title",
            "repo",
            "site_url",
            "org_chat_url",
            "spec_url",
            "spec_file_id",
            "spec_file_name",
            "place",
        }
    )

    async def update_fields(self, hack_id: int, **fields: object) -> None:
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValueError(f"cannot update fields: {unknown}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self.db.execute(
            f"UPDATE hackathons SET {assignments} WHERE id = ?",
            (*fields.values(), hack_id),
        )

    async def delete(self, hack_id: int) -> None:
        await self.db.execute("DELETE FROM hackathons WHERE id = ?", (hack_id,))

    async def archive(
        self, hack_id: int, finished_at: datetime, notification: str | None = None
    ) -> None:
        """Move a hackathon to the archive.

        Open (not closed) tasks are dropped, closed ones stay for statistics.
        Only history-relevant data is kept: materials are cleared.
        """
        statements = [
            (
                """
                    UPDATE hackathons SET status = ?, finished_at = ?,
                        site_url = NULL, org_chat_url = NULL, spec_url = NULL,
                        spec_file_id = NULL, spec_file_name = NULL
                    WHERE id = ?
                    """,
                (HACK_FINISHED, to_db(finished_at), hack_id),
            ),
            (
                "DELETE FROM tasks WHERE hackathon_id = ? AND status != ?",
                (hack_id, TASK_CLOSED),
            ),
            ("UPDATE tasks SET active = 0 WHERE hackathon_id = ?", (hack_id,)),
            ("DELETE FROM deferred_pings WHERE hackathon_id = ?", (hack_id,)),
            ("UPDATE hack_publications SET dirty=dirty+1 WHERE hackathon_id=?", (hack_id,)),
        ]
        if notification:
            statements.append(
                (
                    "INSERT OR IGNORE INTO hack_notices(event_key,hackathon_id,text) VALUES(?,?,?)",
                    (f"archive:{hack_id}", hack_id, notification),
                )
            )
        await self.db.run_in_transaction(statements)

    # --- members ---

    async def add_member(self, hack_id: int, user_id: int) -> bool:
        before = await self.is_member(hack_id, user_id)
        if before:
            return False
        await self.db.execute(
            "INSERT OR IGNORE INTO hackathon_members (hackathon_id, user_id, added_at) "
            "VALUES (?, ?, ?)",
            (hack_id, user_id, to_db(utcnow())),
        )
        return True

    async def remove_member(self, hack_id: int, user_id: int) -> bool:
        removed = await self.db.execute(
            "DELETE FROM hackathon_members WHERE hackathon_id = ? AND user_id = ?",
            (hack_id, user_id),
        )
        await self.db.execute(
            "DELETE FROM deferred_pings WHERE hackathon_id = ? AND user_id = ?",
            (hack_id, user_id),
        )
        return removed > 0

    async def is_member(self, hack_id: int, user_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM hackathon_members WHERE hackathon_id = ? AND user_id = ?",
            (hack_id, user_id),
        )
        return row is not None

    async def members(self, hack_id: int) -> list[int]:
        rows = await self.db.fetchall(
            "SELECT user_id FROM hackathon_members WHERE hackathon_id = ? ORDER BY added_at",
            (hack_id,),
        )
        return [r["user_id"] for r in rows]

    async def participation_counts(self, chat_id: int) -> dict[int, tuple[int, int]]:
        """user_id -> (finished hackathons count, all hackathons count)."""
        rows = await self.db.fetchall(
            """
            SELECT m.user_id,
                   SUM(CASE WHEN h.status = ? THEN 1 ELSE 0 END) AS finished,
                   COUNT(*) AS total
            FROM hackathon_members m JOIN hackathons h ON h.id = m.hackathon_id
            WHERE h.chat_id = ?
            GROUP BY m.user_id
            """,
            (HACK_FINISHED, chat_id),
        )
        return {r["user_id"]: (r["finished"], r["total"]) for r in rows}


class GithubLinksRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_login(self, user_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT github_login FROM github_links WHERE user_id = ?", (user_id,)
        )
        return row["github_login"] if row else None

    async def get_user_by_login(self, login: str) -> int | None:
        row = await self.db.fetchone(
            "SELECT user_id FROM github_links WHERE github_login = ? COLLATE NOCASE", (login,)
        )
        return row["user_id"] if row else None

    async def set_login(self, user_id: int, login: str) -> None:
        await self.db.run_in_transaction(
            [
                ("DELETE FROM github_links WHERE user_id = ?", (user_id,)),
                (
                    "INSERT INTO github_links (user_id, github_login, updated_at) VALUES (?, ?, ?)",
                    (user_id, login, to_db(utcnow())),
                ),
            ]
        )

    async def remove(self, user_id: int) -> bool:
        return await self.db.execute("DELETE FROM github_links WHERE user_id = ?", (user_id,)) > 0

    async def logins_for(self, user_ids: list[int]) -> dict[int, str]:
        if not user_ids:
            return {}
        marks = ",".join("?" for _ in user_ids)
        rows = await self.db.fetchall(
            f"SELECT user_id, github_login FROM github_links WHERE user_id IN ({marks})",
            user_ids,
        )
        return {r["user_id"]: r["github_login"] for r in rows}


class TasksRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, hack_id: int, user_id: int, issue_number: int) -> Task | None:
        row = await self.db.fetchone(
            "SELECT * FROM tasks WHERE hackathon_id = ? AND user_id = ? AND issue_number = ?",
            (hack_id, user_id, issue_number),
        )
        return _task(row) if row else None

    async def upsert_active(
        self,
        hack_id: int,
        user_id: int,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        status: str,
        source: str,
        seen_assigned: bool,
    ) -> Task:
        """Create an active task or re-activate an existing (e.g. previously closed) one."""
        now = to_db(utcnow())
        await self.db.execute(
            """
            INSERT INTO tasks (hackathon_id, user_id, issue_number, issue_title, issue_url,
                               status, active, seen_assigned, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT (hackathon_id, user_id, issue_number) DO UPDATE SET
                issue_title = excluded.issue_title,
                issue_url = excluded.issue_url,
                status = excluded.status,
                active = 1,
                seen_assigned = MAX(tasks.seen_assigned, excluded.seen_assigned),
                closed_at = NULL,
                updated_at = excluded.updated_at
            """,
            (
                hack_id,
                user_id,
                issue_number,
                issue_title,
                issue_url,
                status,
                int(seen_assigned),
                source,
                now,
                now,
            ),
        )
        task = await self.get(hack_id, user_id, issue_number)
        assert task is not None
        return task

    async def update_status(self, task_id: int, status: str, *, title: str | None = None) -> None:
        now = utcnow()
        if status == TASK_CLOSED:
            await self.db.execute(
                "UPDATE tasks SET status = ?, active = 0, closed_at = ?, updated_at = ?, "
                "issue_title = COALESCE(?, issue_title) WHERE id = ?",
                (status, to_db(now), to_db(now), title, task_id),
            )
        else:
            await self.db.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, "
                "issue_title = COALESCE(?, issue_title) WHERE id = ?",
                (status, to_db(now), title, task_id),
            )

    async def mark_seen_assigned(self, task_id: int) -> None:
        await self.db.execute("UPDATE tasks SET seen_assigned = 1 WHERE id = ?", (task_id,))

    async def delete(self, task_id: int) -> None:
        await self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    async def delete_active_for_user(self, hack_id: int, user_id: int) -> int:
        return await self.db.execute(
            "DELETE FROM tasks WHERE hackathon_id = ? AND user_id = ? AND active = 1",
            (hack_id, user_id),
        )

    async def active_for_hackathon(self, hack_id: int) -> list[Task]:
        rows = await self.db.fetchall(
            "SELECT * FROM tasks WHERE hackathon_id = ? AND active = 1 "
            "ORDER BY user_id, issue_number",
            (hack_id,),
        )
        return [_task(r) for r in rows]

    async def active_for_user(self, user_id: int, hack_ids: list[int]) -> list[Task]:
        if not hack_ids:
            return []
        marks = ",".join("?" for _ in hack_ids)
        rows = await self.db.fetchall(
            f"SELECT * FROM tasks WHERE user_id = ? AND active = 1 AND hackathon_id IN ({marks}) "
            "ORDER BY hackathon_id, issue_number",
            (user_id, *hack_ids),
        )
        return [_task(r) for r in rows]

    async def closed_counts(self, hack_id: int) -> tuple[int, dict[int, int]]:
        """(distinct closed issues, closed issues per user) for a hackathon."""
        total_row = await self.db.fetchone(
            "SELECT COUNT(DISTINCT issue_number) AS n FROM tasks "
            "WHERE hackathon_id = ? AND status = ?",
            (hack_id, TASK_CLOSED),
        )
        rows = await self.db.fetchall(
            "SELECT user_id, COUNT(*) AS n FROM tasks WHERE hackathon_id = ? AND status = ? "
            "GROUP BY user_id",
            (hack_id, TASK_CLOSED),
        )
        return (total_row["n"] if total_row else 0), {r["user_id"]: r["n"] for r in rows}

    async def closed_counts_by_user(self, chat_id: int) -> dict[int, int]:
        rows = await self.db.fetchall(
            """
            SELECT t.user_id, COUNT(*) AS n
            FROM tasks t JOIN hackathons h ON h.id = t.hackathon_id
            WHERE h.chat_id = ? AND t.status = ?
            GROUP BY t.user_id
            """,
            (chat_id, TASK_CLOSED),
        )
        return {r["user_id"]: r["n"] for r in rows}


class AvailabilityRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_raw(self, user_id: int) -> dict[int, tuple[str, str]]:
        rows = await self.db.fetchall(
            "SELECT weekday, mode, windows FROM availability WHERE user_id = ?", (user_id,)
        )
        return {r["weekday"]: (r["mode"], r["windows"]) for r in rows}

    async def set_days(self, user_id: int, days: dict[int, tuple[str, str]]) -> None:
        await self.db.run_in_transaction(
            [
                (
                    """
                    INSERT INTO availability (user_id, weekday, mode, windows) VALUES (?, ?, ?, ?)
                    ON CONFLICT (user_id, weekday) DO UPDATE SET
                        mode = excluded.mode, windows = excluded.windows
                    """,
                    (user_id, weekday, mode, windows),
                )
                for weekday, (mode, windows) in days.items()
            ]
        )

    async def reset(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM availability WHERE user_id = ?", (user_id,))


class RemindersRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def sent_offsets(self, hack_id: int, milestone: str, milestone_at: datetime) -> set[int]:
        rows = await self.db.fetchall(
            "SELECT offset_minutes FROM reminders_sent "
            "WHERE hackathon_id = ? AND milestone = ? AND milestone_at = ?",
            (hack_id, milestone, to_db(milestone_at)),
        )
        return {r["offset_minutes"] for r in rows}

    async def mark_sent(
        self, hack_id: int, milestone: str, milestone_at: datetime, offsets: list[int]
    ) -> None:
        now = to_db(utcnow())
        await self.db.run_in_transaction(
            [
                (
                    "INSERT OR IGNORE INTO reminders_sent "
                    "(hackathon_id, milestone, offset_minutes, milestone_at, sent_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (hack_id, milestone, offset, to_db(milestone_at), now),
                )
                for offset in offsets
            ]
        )

    async def add_deferred(
        self, hack_id: int, milestone: str, milestone_at: datetime, user_id: int, due_at: datetime
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO deferred_pings
                (hackathon_id, milestone, milestone_at, user_id, due_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (hackathon_id, milestone, user_id) DO UPDATE SET
                milestone_at = excluded.milestone_at,
                due_at = excluded.due_at,
                created_at = excluded.created_at
            """,
            (hack_id, milestone, to_db(milestone_at), user_id, to_db(due_at), to_db(utcnow())),
        )

    async def list_deferred(self) -> list[DeferredPing]:
        rows = await self.db.fetchall("SELECT * FROM deferred_pings ORDER BY due_at")
        return [
            DeferredPing(
                hackathon_id=r["hackathon_id"],
                milestone=r["milestone"],
                milestone_at=from_db(r["milestone_at"]),
                user_id=r["user_id"],
                due_at=from_db(r["due_at"]),
            )
            for r in rows
        ]

    async def delete_deferred(self, hack_id: int, milestone: str, user_id: int) -> None:
        await self.db.execute(
            "DELETE FROM deferred_pings WHERE hackathon_id = ? AND milestone = ? AND user_id = ?",
            (hack_id, milestone, user_id),
        )


class PopupsRepo:
    """Texts of short answers; a button in the chat refers to them by id."""

    TTL = timedelta(days=30)

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, chat_id: int, text: str) -> int:
        now = utcnow()
        await self.db.execute("DELETE FROM popups WHERE created_at < ?", (to_db(now - self.TTL),))
        return await self.db.execute(
            "INSERT INTO popups (chat_id, text, created_at) VALUES (?, ?, ?)",
            (chat_id, text, to_db(now)),
        )

    async def get(self, popup_id: int, chat_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT text FROM popups WHERE id = ? AND chat_id = ?", (popup_id, chat_id)
        )
        return row["text"] if row else None


class Storage:
    """Facade bundling all repositories."""

    def __init__(self, db: Database, default_timezone: str) -> None:
        self.db = db
        self.users = UsersRepo(db)
        self.chats = ChatsRepo(db, default_timezone)
        self.hackathons = HackathonsRepo(db)
        self.github_links = GithubLinksRepo(db)
        self.tasks = TasksRepo(db)
        self.availability = AvailabilityRepo(db)
        self.reminders = RemindersRepo(db)
        self.popups = PopupsRepo(db)
        self.workflow = HackWorkflowRepo(db)
