"""Versioned schema migrations.

Every entry in MIGRATIONS upgrades the schema by exactly one version. Never edit
an already released migration: append a new one instead, so that existing
databases are upgraded in place on the next start.
"""

from __future__ import annotations

import logging

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE users (
        user_id     INTEGER PRIMARY KEY,
        username    TEXT,
        first_name  TEXT NOT NULL DEFAULT '',
        last_name   TEXT,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX idx_users_username ON users (username COLLATE NOCASE);

    CREATE TABLE chats (
        chat_id   INTEGER PRIMARY KEY,
        timezone  TEXT NOT NULL
    );

    CREATE TABLE chat_migrations (
        old_chat_id  INTEGER PRIMARY KEY,
        new_chat_id  INTEGER NOT NULL,
        migrated_at  TEXT NOT NULL
    );

    CREATE TABLE hackathons (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id           INTEGER NOT NULL,
        slug              TEXT NOT NULL,
        title             TEXT,
        registration_end  TEXT NOT NULL,
        start_at          TEXT NOT NULL,
        end_at            TEXT NOT NULL,
        repo              TEXT,
        site_url          TEXT,
        org_chat_url      TEXT,
        spec_url          TEXT,
        spec_file_id      TEXT,
        spec_file_name    TEXT,
        status            TEXT NOT NULL DEFAULT 'active',
        place             INTEGER,
        created_at        TEXT NOT NULL,
        finished_at       TEXT,
        UNIQUE (chat_id, slug)
    );

    CREATE TABLE hackathon_members (
        hackathon_id  INTEGER NOT NULL REFERENCES hackathons (id) ON DELETE CASCADE,
        user_id       INTEGER NOT NULL,
        added_at      TEXT NOT NULL,
        PRIMARY KEY (hackathon_id, user_id)
    );

    CREATE TABLE github_links (
        user_id       INTEGER PRIMARY KEY,
        github_login  TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE UNIQUE INDEX idx_github_links_login ON github_links (github_login COLLATE NOCASE);

    CREATE TABLE tasks (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        hackathon_id   INTEGER NOT NULL REFERENCES hackathons (id) ON DELETE CASCADE,
        user_id        INTEGER NOT NULL,
        issue_number   INTEGER NOT NULL,
        issue_title    TEXT NOT NULL DEFAULT '',
        issue_url      TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL,
        active         INTEGER NOT NULL DEFAULT 1,
        seen_assigned  INTEGER NOT NULL DEFAULT 0,
        source         TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        closed_at      TEXT,
        UNIQUE (hackathon_id, user_id, issue_number)
    );

    CREATE TABLE availability (
        user_id  INTEGER NOT NULL,
        weekday  INTEGER NOT NULL,
        mode     TEXT NOT NULL,
        windows  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, weekday)
    );

    CREATE TABLE reminders_sent (
        hackathon_id    INTEGER NOT NULL REFERENCES hackathons (id) ON DELETE CASCADE,
        milestone       TEXT NOT NULL,
        offset_minutes  INTEGER NOT NULL,
        milestone_at    TEXT NOT NULL,
        sent_at         TEXT NOT NULL,
        PRIMARY KEY (hackathon_id, milestone, offset_minutes, milestone_at)
    );

    CREATE TABLE deferred_pings (
        hackathon_id    INTEGER NOT NULL REFERENCES hackathons (id) ON DELETE CASCADE,
        milestone       TEXT NOT NULL,
        milestone_at    TEXT NOT NULL,
        user_id         INTEGER NOT NULL,
        due_at          TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (hackathon_id, milestone, user_id)
    );
    """,
    # 2: short command answers shown in a popup behind a button
    """
    CREATE TABLE popups (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     INTEGER NOT NULL,
        text        TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    """,
    # 3: self-service hackathons, persistent private conversations and team formation.
    """
    ALTER TABLE hackathons ADD COLUMN created_by INTEGER;
    ALTER TABLE hackathons ADD COLUMN team_min INTEGER NOT NULL DEFAULT 1 CHECK(team_min >= 1);
    ALTER TABLE hackathons ADD COLUMN team_max INTEGER NOT NULL DEFAULT 4
        CHECK(team_max >= team_min);
    ALTER TABLE hackathons ADD COLUMN source_key TEXT;
    ALTER TABLE hackathons ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
    CREATE UNIQUE INDEX idx_hackathon_source ON hackathons(chat_id, source_key)
        WHERE source_key IS NOT NULL;

    CREATE TABLE hack_sessions (
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        token TEXT PRIMARY KEY,
        step TEXT NOT NULL,
        data TEXT NOT NULL DEFAULT '{}',
        expires_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_hack_session_user ON hack_sessions(user_id, updated_at);
    CREATE TABLE hack_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hackathon_id INTEGER NOT NULL REFERENCES hackathons(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        at TEXT NOT NULL,
        UNIQUE(hackathon_id, title, at)
    );
    CREATE TABLE hack_votes (
        hackathon_id INTEGER NOT NULL REFERENCES hackathons(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        choice TEXT NOT NULL CHECK(choice IN ('yes', 'no', 'maybe')),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(hackathon_id, user_id)
    );
    CREATE TABLE hack_teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hackathon_id INTEGER NOT NULL REFERENCES hackathons(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(id, hackathon_id)
    );
    CREATE TABLE hack_team_members (
        hackathon_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        team_id INTEGER NOT NULL,
        PRIMARY KEY(hackathon_id, user_id),
        FOREIGN KEY(team_id, hackathon_id) REFERENCES hack_teams(id, hackathon_id) ON DELETE CASCADE
    );
    CREATE TABLE hack_publications (
        hackathon_id INTEGER PRIMARY KEY REFERENCES hackathons(id) ON DELETE CASCADE,
        message_id INTEGER,
        dirty INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE hack_notices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL UNIQUE,
        hackathon_id INTEGER NOT NULL REFERENCES hackathons(id) ON DELETE CASCADE,
        text TEXT NOT NULL,
        delivered_at TEXT
    );
    """,
]

LATEST_VERSION = len(MIGRATIONS)


async def get_version(conn: aiosqlite.Connection) -> int:
    await conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    async with conn.execute("SELECT version FROM schema_version") as cur:
        row = await cur.fetchone()
    if row is None:
        await conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        await conn.commit()
        return 0
    return int(row[0])


async def migrate(conn: aiosqlite.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    version = await get_version(conn)
    if version > LATEST_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than this code supports "
            f"({LATEST_VERSION}). Update the bot."
        )
    while version < LATEST_VERSION:
        script = MIGRATIONS[version]
        target = version + 1
        log.info("Applying database migration %d", target)
        await conn.executescript(
            f"BEGIN;\n{script}\nUPDATE schema_version SET version = {target};\nCOMMIT;"
        )
        version = target
    return version
