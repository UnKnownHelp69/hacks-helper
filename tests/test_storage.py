from datetime import UTC, datetime, timedelta

from bot.db.database import Database
from bot.db.migrations import LATEST_VERSION, get_version, migrate
from bot.db.models import HACK_FINISHED, TASK_CLOSED, TASK_NOT_STARTED, TASK_SOURCE_BOT
from bot.db.repositories import Storage
from tests.conftest import ALLOWED_CHAT

START = datetime(2026, 11, 5, 7, 0, tzinfo=UTC)


async def test_migrations_are_idempotent():
    db = Database(":memory:")
    await db.connect()
    assert await get_version(db.conn) == LATEST_VERSION
    assert await migrate(db.conn) == LATEST_VERSION
    await db.close()


async def test_registry_username_change(storage):
    await storage.users.upsert(1, "alice", "Alice", None)
    await storage.users.upsert(1, "alice_new", "Alice", None)
    assert (await storage.users.get(1)).username == "alice_new"
    assert await storage.users.find_by_username("alice") is None
    assert (await storage.users.find_by_username("@ALICE_new")).user_id == 1


async def test_registry_username_taken_over_by_another_id(storage):
    await storage.users.upsert(1, "nick", "Old", None)
    await storage.users.upsert(2, "Nick", "New", None)  # case-insensitive
    assert (await storage.users.get(1)).username is None
    assert (await storage.users.find_by_username("nick")).user_id == 2


async def test_registry_user_removed_username(storage):
    await storage.users.upsert(1, "nick", "Old", None)
    await storage.users.upsert(1, None, "Old", None)
    assert (await storage.users.get(1)).username is None


async def test_chat_migration_moves_hackathons_and_timezone(storage):
    await storage.chats.set_timezone(ALLOWED_CHAT, "Asia/Yekaterinburg")
    hack = await storage.hackathons.create(
        ALLOWED_CHAT, "h1", None, START, START, START + timedelta(days=1)
    )
    new_chat = ALLOWED_CHAT - 1_000_000
    await storage.chats.migrate_chat(ALLOWED_CHAT, new_chat)
    assert (await storage.hackathons.get_by_id(hack.id)).chat_id == new_chat
    assert await storage.chats.get_timezone(new_chat) == "Asia/Yekaterinburg"
    assert await storage.chats.get_migrations() == {ALLOWED_CHAT: new_chat}
    # running it twice is harmless
    await storage.chats.migrate_chat(ALLOWED_CHAT, new_chat)
    assert (await storage.hackathons.get(new_chat, "h1")) is not None


async def test_archive_keeps_closed_tasks_and_history(storage):
    hack = await storage.hackathons.create(
        ALLOWED_CHAT, "h1", "Title", START, START, START + timedelta(days=1)
    )
    await storage.hackathons.update_fields(hack.id, repo="o/r", site_url="https://x.example")
    await storage.hackathons.add_member(hack.id, 1)
    await storage.hackathons.add_member(hack.id, 2)
    t1 = await storage.tasks.upsert_active(
        hack.id, 1, 10, "a", "", TASK_NOT_STARTED, TASK_SOURCE_BOT, False
    )
    t2 = await storage.tasks.upsert_active(
        hack.id, 2, 11, "b", "", TASK_NOT_STARTED, TASK_SOURCE_BOT, False
    )
    await storage.tasks.update_status(t1.id, TASK_CLOSED)

    await storage.hackathons.archive(hack.id, START + timedelta(days=1))

    archived = await storage.hackathons.get_by_id(hack.id)
    assert archived.status == HACK_FINISHED
    assert archived.repo == "o/r"  # kept for history
    assert archived.site_url is None  # materials are not part of the history
    assert await storage.hackathons.members(hack.id) == [1, 2]
    assert await storage.tasks.active_for_hackathon(hack.id) == []
    assert await storage.tasks.get(hack.id, 2, t2.issue_number) is None
    total, per_user = await storage.tasks.closed_counts(hack.id)
    assert (total, per_user) == (1, {1: 1})
    counts = await storage.hackathons.participation_counts(ALLOWED_CHAT)
    assert counts == {1: (1, 1), 2: (1, 1)}


async def test_reopened_task_is_reactivated(storage):
    hack = await storage.hackathons.create(
        ALLOWED_CHAT, "h1", None, START, START, START + timedelta(days=1)
    )
    task = await storage.tasks.upsert_active(
        hack.id, 1, 10, "a", "", TASK_NOT_STARTED, TASK_SOURCE_BOT, True
    )
    await storage.tasks.update_status(task.id, TASK_CLOSED)
    again = await storage.tasks.upsert_active(
        hack.id, 1, 10, "a", "", TASK_NOT_STARTED, TASK_SOURCE_BOT, False
    )
    assert again.id == task.id and again.active and again.closed_at is None
    assert again.seen_assigned  # never downgraded


async def test_existing_v1_database_is_upgraded(tmp_path):
    import aiosqlite

    from bot.db.migrations import MIGRATIONS

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript(MIGRATIONS[0])
        await conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        await conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        await conn.execute(
            "INSERT INTO chats (chat_id, timezone) VALUES (?, ?)", (ALLOWED_CHAT, "Asia/Omsk")
        )
        await conn.commit()
    db = Database(path)
    await db.connect()
    assert await get_version(db.conn) == LATEST_VERSION
    storage_ = Storage(db, "Europe/Moscow")
    assert await storage_.chats.get_timezone(ALLOWED_CHAT) == "Asia/Omsk"  # data kept
    assert await storage_.popups.add(ALLOWED_CHAT, "x") > 0
    await db.close()


async def test_existing_v2_hackathon_and_members_survive_workflow_migration(tmp_path):
    import aiosqlite

    from bot.db.migrations import MIGRATIONS

    path = tmp_path / "v2.db"
    async with aiosqlite.connect(path) as conn:
        await conn.executescript("\n".join(MIGRATIONS[:2]))
        await conn.executescript(
            "CREATE TABLE schema_version(version INTEGER NOT NULL);"
            "INSERT INTO schema_version VALUES(2);"
            "INSERT INTO hackathons"
            "(chat_id,slug,title,registration_end,start_at,end_at,created_at) "
            "VALUES(-1001,'old','Сохранённый хакатон','2026-11-01T00:00:00+00:00',"
            "'2026-11-05T00:00:00+00:00','2026-11-07T00:00:00+00:00',"
            "'2026-10-01T00:00:00+00:00');"
            "INSERT INTO hackathon_members VALUES(1,123,'2026-10-01T00:00:00+00:00');"
        )
        await conn.commit()
    db = Database(path)
    await db.connect()
    try:
        storage = Storage(db, "Europe/Moscow")
        hack = await storage.hackathons.get(-1001, "old")
        assert hack.title == "Сохранённый хакатон"
        assert hack.team_min == 1 and hack.team_max == 4 and hack.created_by is None
        assert await storage.hackathons.members(hack.id) == [123]
        assert await storage.workflow.checkpoints(hack.id) == []
        assert await get_version(db.conn) == LATEST_VERSION
    finally:
        await db.close()
