"""Persistent personal sessions and atomic self-service voting/team transitions."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

import aiosqlite

from bot.db.database import Database, from_db, to_db, utcnow
from bot.db.models import Checkpoint, HackSession, Team
from bot.errors import UserError


async def _one(conn: aiosqlite.Connection, sql: str, params=()) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchone()


class HackWorkflowRepo:
    FIELDS = frozenset(
        {
            "title",
            "registration_end",
            "start_at",
            "end_at",
            "repo",
            "site_url",
            "org_chat_url",
            "spec_url",
            "spec_file_id",
            "spec_file_name",
            "team_min",
            "team_max",
        }
    )

    def __init__(self, db: Database) -> None:
        self.db = db

    async def new_session(
        self, user_id: int, chat_id: int, step: str = "menu", data: dict[str, Any] | None = None
    ) -> HackSession:
        now = utcnow()
        session = HackSession(
            user_id, chat_id, secrets.token_urlsafe(6), step, data or {}, now + timedelta(days=1)
        )
        await self.db.run_in_transaction(
            [
                ("DELETE FROM hack_sessions WHERE expires_at <= ?", (to_db(now),)),
                (
                    "INSERT INTO hack_sessions"
                    "(user_id,chat_id,token,step,data,expires_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        user_id,
                        chat_id,
                        session.token,
                        step,
                        json.dumps(session.data),
                        to_db(session.expires_at),
                        datetime.now(UTC).isoformat(),
                    ),
                ),
            ]
        )
        return session

    @staticmethod
    def _session(row: aiosqlite.Row) -> HackSession:
        return HackSession(
            row["user_id"],
            row["chat_id"],
            row["token"],
            row["step"],
            json.loads(row["data"]),
            from_db(row["expires_at"]),
            row["revision"],
        )

    async def session(
        self, user_id: int, token: str | None = None, *, chat_id: int | None = None
    ) -> HackSession | None:
        sql = "SELECT * FROM hack_sessions WHERE user_id = ? AND expires_at > ?"
        params: tuple = (user_id, to_db(utcnow()))
        if token is not None:
            sql += " AND token = ?"
            params += (token,)
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params += (chat_id,)
        row = await self.db.fetchone(sql + " ORDER BY updated_at DESC, rowid DESC LIMIT 1", params)
        if row is None:
            return None
        return self._session(row)

    async def delete_session(self, session: HackSession) -> None:
        await self.db.execute(
            "DELETE FROM hack_sessions WHERE token=? AND user_id=?",
            (session.token, session.user_id),
        )

    async def clear_forms(self, user_id: int, chat_id: int) -> None:
        await self.db.execute(
            "DELETE FROM hack_sessions WHERE user_id=? AND chat_id=? "
            "AND (step LIKE 'form:%' OR step IN ('editing','edit_checkpoints'))",
            (user_id, chat_id),
        )

    async def clear_unfinished_forms(self) -> None:
        """Remove retired drafts, retaining published hackathons and personal settings."""
        await self.db.execute(
            "DELETE FROM hack_sessions WHERE step IN "
            "('preview','checkpoints','editing','edit_checkpoints','completing','saving') "
            "OR step LIKE 'input:%' OR step LIKE 'form:%'"
        )

    async def save_session(self, session: HackSession) -> None:
        changed = await self.db.execute(
            "UPDATE hack_sessions SET step=?,data=?,updated_at=?,revision=revision+1 "
            "WHERE token=? AND user_id=? AND revision=? AND expires_at > ?",
            (
                session.step,
                json.dumps(session.data),
                datetime.now(UTC).isoformat(),
                session.token,
                session.user_id,
                session.revision,
                to_db(utcnow()),
            ),
        )
        if not changed:
            raise UserError("Это действие уже обработано или диалог устарел. Откройте /hack")
        session.revision += 1

    async def bind_prompt(self, session: HackSession, message_id: int) -> bool:
        """Bind delivery metadata without invalidating the buttons just rendered."""
        changed = await self.db.execute(
            "UPDATE hack_sessions SET data=json_set(data,'$._prompt_id',?) "
            "WHERE token=? AND user_id=? AND revision=? AND expires_at>?",
            (message_id, session.token, session.user_id, session.revision, to_db(utcnow())),
        )
        if changed:
            session.data["_prompt_id"] = message_id
        # A newer interaction may already have advanced or closed this form.
        return bool(changed)

    async def checkpoints(self, hack_id: int) -> list[Checkpoint]:
        rows = await self.db.fetchall(
            "SELECT * FROM hack_checkpoints WHERE hackathon_id=? ORDER BY at,id", (hack_id,)
        )
        return [Checkpoint(row["id"], hack_id, row["title"], from_db(row["at"])) for row in rows]

    async def teams(self, hack_id: int) -> list[Team]:
        rows = await self.db.fetchall(
            "SELECT * FROM hack_teams WHERE hackathon_id=? ORDER BY id", (hack_id,)
        )
        memberships = await self.db.fetchall(
            "SELECT * FROM hack_team_members WHERE hackathon_id=? ORDER BY user_id", (hack_id,)
        )
        return [
            Team(
                row["id"],
                hack_id,
                row["name"],
                [member["user_id"] for member in memberships if member["team_id"] == row["id"]],
            )
            for row in rows
        ]

    async def votes(self, hack_id: int) -> dict[str, list[int]]:
        rows = await self.db.fetchall(
            "SELECT user_id,choice FROM hack_votes WHERE hackathon_id=? "
            "ORDER BY updated_at,user_id",
            (hack_id,),
        )
        return {
            choice: [row["user_id"] for row in rows if row["choice"] == choice]
            for choice in ("yes", "no", "maybe")
        }

    async def _active(self, conn, hack_id: int, *, joining: bool = False):
        hack = await _one(conn, "SELECT * FROM hackathons WHERE id=?", (hack_id,))
        if hack is None or hack["status"] != "active" or from_db(hack["end_at"]) <= utcnow():
            raise UserError("Хакатон завершён или недоступен")
        if joining and from_db(hack["registration_end"]) <= utcnow():
            raise UserError("Регистрация на хакатон уже закончилась")
        return hack

    async def _dirty(self, conn, hack_id: int) -> None:
        await conn.execute(
            "INSERT INTO hack_publications(hackathon_id) VALUES(?) "
            "ON CONFLICT(hackathon_id) DO UPDATE SET dirty=dirty+1",
            (hack_id,),
        )

    async def _formed(self, conn, hack, team_id: int) -> None:
        team = await _one(conn, "SELECT name FROM hack_teams WHERE id=?", (team_id,))
        await conn.execute(
            "INSERT OR IGNORE INTO hack_notices(event_key,hackathon_id,text) VALUES(?,?,?)",
            (
                f"team:{team_id}:formed",
                hack["id"],
                f"👥 {escape(team['name'])} для <b>{escape(hack['title'] or hack['slug'])}</b> "
                "создана. Состав — в карточке хакатона.",
            ),
        )

    async def _new_team(self, conn, hack_id: int) -> int:
        row = await _one(
            conn, "SELECT COUNT(*) AS n FROM hack_teams WHERE hackathon_id=?", (hack_id,)
        )
        cursor = await conn.execute(
            "INSERT INTO hack_teams(hackathon_id,name,created_at) VALUES(?,?,?)",
            (hack_id, f"Команда {row['n'] + 1}", to_db(utcnow())),
        )
        return cursor.lastrowid

    async def vote(self, hack_id: int, user_id: int, choice: str) -> int | None:
        if choice not in {"yes", "no", "maybe"}:
            raise UserError("Неизвестный вариант голосования")
        async with self.db.transaction() as conn:
            hack = await self._active(conn, hack_id, joining=choice == "yes")
            await conn.execute(
                "INSERT INTO hack_votes(hackathon_id,user_id,choice,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(hackathon_id,user_id) DO UPDATE SET "
                "choice=excluded.choice,updated_at=excluded.updated_at",
                (hack_id, user_id, choice, to_db(utcnow())),
            )
            if choice != "yes":
                await conn.execute(
                    "DELETE FROM hack_team_members WHERE hackathon_id=? AND user_id=?",
                    (hack_id, user_id),
                )
                await conn.execute(
                    "DELETE FROM hackathon_members WHERE hackathon_id=? AND user_id=?",
                    (hack_id, user_id),
                )
                await conn.execute(
                    "DELETE FROM tasks WHERE hackathon_id=? AND user_id=? AND active=1",
                    (hack_id, user_id),
                )
                await conn.execute(
                    "DELETE FROM deferred_pings WHERE hackathon_id=? AND user_id=?",
                    (hack_id, user_id),
                )
                await self._dirty(conn, hack_id)
                return None
            await conn.execute(
                "INSERT OR IGNORE INTO hackathon_members"
                "(hackathon_id,user_id,added_at) VALUES(?,?,?)",
                (hack_id, user_id, to_db(utcnow())),
            )
            existing = await _one(
                conn,
                "SELECT team_id FROM hack_team_members WHERE hackathon_id=? AND user_id=?",
                (hack_id, user_id),
            )
            team_id = existing["team_id"] if existing else None
            if team_id is None:
                recruiting = await _one(
                    conn,
                    "SELECT t.id FROM hack_teams t LEFT JOIN hack_team_members m ON m.team_id=t.id "
                    "WHERE t.hackathon_id=? GROUP BY t.id "
                    "HAVING COUNT(m.user_id) < ? ORDER BY t.id LIMIT 1",
                    (hack_id, hack["team_max"]),
                )
                any_team = await _one(
                    conn, "SELECT id FROM hack_teams WHERE hackathon_id=? LIMIT 1", (hack_id,)
                )
                if recruiting:
                    team_id = recruiting["id"]
                elif any_team is None:
                    team_id = await self._new_team(conn, hack_id)
                if team_id is not None:
                    await conn.execute(
                        "INSERT INTO hack_team_members(hackathon_id,user_id,team_id) VALUES(?,?,?)",
                        (hack_id, user_id, team_id),
                    )
            if team_id is not None:
                await self._formed(conn, hack, team_id)
            await self._dirty(conn, hack_id)
            return team_id

    async def join_team(
        self, hack_id: int, user_id: int, team_id: int | None, *, overflow: bool = False
    ) -> int:
        async with self.db.transaction() as conn:
            hack = await self._active(conn, hack_id, joining=True)
            vote = await _one(
                conn,
                "SELECT choice FROM hack_votes WHERE hackathon_id=? AND user_id=?",
                (hack_id, user_id),
            )
            if vote is None or vote["choice"] != "yes":
                raise UserError("Сначала выберите «Участвую»")
            current = await _one(
                conn,
                "SELECT team_id FROM hack_team_members WHERE hackathon_id=? AND user_id=?",
                (hack_id, user_id),
            )
            if team_id is not None:
                team = await _one(
                    conn,
                    "SELECT id FROM hack_teams WHERE id=? AND hackathon_id=?",
                    (team_id, hack_id),
                )
                if team is None:
                    raise UserError("Эта команда недоступна")
                if current and current["team_id"] == team_id:
                    return team_id
                size = await _one(
                    conn, "SELECT COUNT(*) AS n FROM hack_team_members WHERE team_id=?", (team_id,)
                )
                if size["n"] >= hack["team_max"] and not overflow:
                    raise UserError(
                        "Команда заполнена. Выберите присоединение сверх лимита или новую команду"
                    )
            else:
                if current:
                    raise UserError("Вы уже состоите в команде")
                available = await _one(
                    conn,
                    "SELECT t.id FROM hack_teams t LEFT JOIN hack_team_members m ON m.team_id=t.id "
                    "WHERE t.hackathon_id=? GROUP BY t.id HAVING COUNT(m.user_id) < ? LIMIT 1",
                    (hack_id, hack["team_max"]),
                )
                if available:
                    raise UserError("В командах ещё есть места. Выберите существующую команду")
                team_id = await self._new_team(conn, hack_id)
            await conn.execute(
                "INSERT INTO hack_team_members(hackathon_id,user_id,team_id) VALUES(?,?,?) "
                "ON CONFLICT(hackathon_id,user_id) DO UPDATE SET team_id=excluded.team_id",
                (hack_id, user_id, team_id),
            )
            await self._formed(conn, hack, team_id)
            await self._dirty(conn, hack_id)
            return team_id

    async def save_hack(self, session: HackSession, fields: dict[str, Any]) -> int:
        """Publish or edit once; concurrent clicks cannot create duplicate hackathons."""
        if set(fields) - self.FIELDS:
            raise ValueError("Unknown hackathon fields")
        async with self.db.transaction() as conn:
            record = await _one(
                conn,
                "SELECT * FROM hack_sessions WHERE token=? AND user_id=?",
                (session.token, session.user_id),
            )
            if record is None or from_db(record["expires_at"]) <= utcnow():
                raise UserError("Диалог устарел. Откройте /hack")
            stored = json.loads(record["data"])
            if stored.get("published_id"):
                return int(stored["published_id"])
            if record["revision"] != session.revision:
                raise UserError("Данные изменились. Откройте хакатон заново")
            hack_id = session.data.get("editing_id")
            if hack_id is not None:
                hack = await self._active(conn, int(hack_id))
                if hack["chat_id"] != session.chat_id or hack["revision"] != session.data.get(
                    "editing_revision"
                ):
                    raise UserError("Карточка изменилась. Откройте её заново перед редактированием")
                columns = ",".join(f"{key}=?" for key in fields)
                await conn.execute(
                    f"UPDATE hackathons SET {columns},revision=revision+1 WHERE id=?",
                    (*fields.values(), hack_id),
                )
                wanted = {
                    (item["title"], item["at"]) for item in session.data.get("checkpoints", [])
                }
                async with conn.execute(
                    "SELECT * FROM hack_checkpoints WHERE hackathon_id=?", (hack_id,)
                ) as cursor:
                    for item in await cursor.fetchall():
                        if (item["title"], item["at"]) not in wanted:
                            await conn.execute(
                                "DELETE FROM hack_checkpoints WHERE id=?", (item["id"],)
                            )
            else:
                source = session.data.get("source_key")
                duplicate = (
                    await _one(
                        conn,
                        "SELECT id FROM hackathons WHERE chat_id=? AND source_key=?",
                        (session.chat_id, source),
                    )
                    if source
                    else None
                )
                if duplicate:
                    hack_id = duplicate["id"]
                    stored["published_id"] = hack_id
                    await conn.execute(
                        "UPDATE hack_sessions SET data=?,step='published',revision=revision+1 "
                        "WHERE token=?",
                        (json.dumps(stored), session.token),
                    )
                    return int(hack_id)
                else:
                    columns = ",".join(fields)
                    placeholders = ",".join("?" for _ in fields)
                    cursor = await conn.execute(
                        "INSERT INTO hackathons"
                        f"(chat_id,slug,created_by,created_at,source_key,{columns}) "
                        f"VALUES(?,?,?,?,?,{placeholders})",
                        (
                            session.chat_id,
                            "h" + secrets.token_hex(4),
                            session.user_id,
                            to_db(utcnow()),
                            source,
                            *fields.values(),
                        ),
                    )
                    hack_id = cursor.lastrowid
            for checkpoint in session.data.get("checkpoints", []):
                await conn.execute(
                    "INSERT OR IGNORE INTO hack_checkpoints(hackathon_id,title,at) VALUES(?,?,?)",
                    (hack_id, checkpoint["title"], checkpoint["at"]),
                )
            current_hack = await _one(conn, "SELECT * FROM hackathons WHERE id=?", (hack_id,))
            async with conn.execute(
                "SELECT id FROM hack_teams WHERE hackathon_id=?", (hack_id,)
            ) as cursor:
                for team in await cursor.fetchall():
                    await self._formed(conn, current_hack, team["id"])
            stored["published_id"] = hack_id
            await conn.execute(
                "UPDATE hack_sessions SET data=?,step='published',revision=revision+1 "
                "WHERE token=?",
                (json.dumps(stored), session.token),
            )
            await self._dirty(conn, hack_id)
            return int(hack_id)

    async def dirty_publications(self) -> list[aiosqlite.Row]:
        return await self.db.fetchall(
            "SELECT p.* FROM hack_publications p JOIN hackathons h ON h.id=p.hackathon_id "
            "WHERE p.dirty>0 ORDER BY p.hackathon_id"
        )

    async def publication_sent(self, hack_id: int, message_id: int, generation: int) -> None:
        await self.db.execute(
            "UPDATE hack_publications SET message_id=?,"
            "dirty=CASE WHEN dirty=? THEN 0 ELSE dirty END "
            "WHERE hackathon_id=?",
            (message_id, generation, hack_id),
        )

    async def pending_notices(self) -> list[aiosqlite.Row]:
        return await self.db.fetchall(
            "SELECT * FROM hack_notices WHERE delivered_at IS NULL ORDER BY id"
        )

    async def notice_sent(self, notice_id: int) -> None:
        await self.db.execute(
            "UPDATE hack_notices SET delivered_at=? WHERE id=?", (to_db(utcnow()), notice_id)
        )
