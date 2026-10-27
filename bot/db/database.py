"""SQLite connection management."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from bot.db.migrations import migrate

log = logging.getLogger(__name__)


def to_db(dt: datetime | None) -> str | None:
    """Serialize an aware datetime as an ISO string in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not allowed in the database")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_db(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class Database:
    """A single shared aiosqlite connection (the bot is the only writer)."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        # Serializes writes so that concurrent coroutines never commit each other's
        # half-finished transactions on the shared connection.
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            await self._conn.execute("PRAGMA journal_mode = WAL")
        version = await migrate(self._conn)
        log.info("Database ready (schema version %d)", version)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Execute a statement and commit. Returns lastrowid (or rowcount for updates)."""
        async with self._write_lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            is_insert = sql.lstrip().upper().startswith("INSERT")
            result = cur.lastrowid if is_insert else cur.rowcount
            await cur.close()
        return result or 0

    async def run_in_transaction(self, statements: Iterable[tuple[str, Iterable[Any]]]) -> None:
        """Execute several statements atomically."""
        async with self._write_lock:
            try:
                for sql, params in statements:
                    await self.conn.execute(sql, tuple(params))
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize a read/modify/write operation, including cancellation-safe rollback."""
        async with self._write_lock:
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())
