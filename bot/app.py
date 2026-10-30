"""Application wiring and startup."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import (
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    LinkPreviewOptions,
)

from bot.config import Settings
from bot.context import AppContext
from bot.db.database import Database
from bot.db.repositories import Storage
from bot.handlers import build_router
from bot.middlewares.registry import UserRegistryMiddleware
from bot.middlewares.security import SecurityMiddleware, status_value
from bot.services.archive import ArchiveService
from bot.services.github import GitHubClient
from bot.services.hack_notifications import HackNotifications
from bot.services.hackathons import HackathonService
from bot.services.notify import Messenger, Pinger
from bot.services.reminders import ReminderService
from bot.services.scheduler import create_scheduler
from bot.services.tasks import TaskService
from bot.services.users import UserDirectory
from bot.texts import bot_commands

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "my_chat_member", "chat_member"]

# Rights an admin bot does not need; their presence is reported at startup.
EXTRA_RIGHTS = (
    "can_delete_messages",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
    "can_manage_video_chats",
    "can_manage_topics",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
    "can_post_messages",
    "can_edit_messages",
)


def build_context(
    settings: Settings, bot: Bot, storage: Storage, github: GitHubClient | None = None
) -> AppContext:
    github = github or GitHubClient(settings.github_token)
    directory = UserDirectory(bot, storage.users, settings.user_cache_ttl_seconds)
    pinger = Pinger(directory, storage.availability)
    messenger = Messenger(bot, settings)
    tasks = TaskService(settings, storage, github, pinger, messenger)
    reminders = ReminderService(settings, storage, pinger, messenger)
    archive = ArchiveService(settings, storage, tasks)
    hacks = HackathonService(settings, storage, directory, github)
    hack_notifications = HackNotifications(settings, storage, bot, directory, messenger)
    return AppContext(
        settings=settings,
        storage=storage,
        github=github,
        directory=directory,
        pinger=pinger,
        messenger=messenger,
        tasks=tasks,
        reminders=reminders,
        archive=archive,
        hacks=hacks,
        hack_notifications=hack_notifications,
    )


def build_dispatcher(
    ctx: AppContext, on_migrate: Callable[[int, int], Awaitable[None]] | None = None
) -> Dispatcher:
    """Dispatcher with security first, then the user registry, then the handlers."""
    dp = Dispatcher(ctx=ctx)
    dp.update.outer_middleware(SecurityMiddleware(ctx.settings, on_migrate))
    dp.update.outer_middleware(UserRegistryMiddleware(ctx.directory))
    dp.include_router(build_router())
    return dp


async def apply_known_migrations(settings: Settings, storage: Storage) -> None:
    """Follow persisted migration chains without authorizing two groups at once."""
    migrations = await storage.chats.get_migrations()
    seen: set[int] = set()
    while settings.allowed_chat_ids:
        old_id = next(iter(settings.allowed_chat_ids))
        if old_id not in migrations:
            return
        new_id = migrations[old_id]
        if old_id in seen or new_id >= 0:
            settings.allowed_chat_ids.clear()
            log.error("Invalid group migration chain; configure ALLOWED_CHAT_IDS again")
            return
        seen.add(old_id)
        settings.allowed_chat_ids = {new_id}
        log.warning("Chat %s migrated to %s; update ALLOWED_CHAT_IDS", old_id, new_id)


async def wait_for_telegram(bot: Bot) -> None:
    """Retry getMe until Telegram is reachable (e.g. the PC booted before the network)."""
    delay = 5
    while True:
        try:
            await bot.get_me()
            return
        except TelegramUnauthorizedError:
            log.error("Telegram rejected BOT_TOKEN: check the token in .env")
            raise
        except Exception as exc:
            log.warning("Telegram is unreachable (%s), retrying in %d s", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


async def check_bot_rights(bot: Bot, settings: Settings) -> None:
    me = await bot.get_me()
    log.info(
        "Running as @%s (id %s); privacy mode %s",
        me.username,
        me.id,
        "off" if me.can_read_all_group_messages else "on",
    )
    for chat_id in sorted(settings.allowed_chat_ids):
        if not settings.is_chat_allowed(chat_id):
            continue
        try:
            member = await bot.get_chat_member(chat_id, me.id)
        except Exception as exc:
            log.error("Cannot access allowed chat %s: %s. Is the bot in that group?", chat_id, exc)
            continue
        status = status_value(member.status)
        if status != "administrator":
            log.warning("Bot is NOT an administrator in chat %s (status: %s)", chat_id, status)
            try:
                from bot.handlers.events import NOT_ADMIN_WARNING

                await bot.send_message(chat_id, NOT_ADMIN_WARNING)
            except Exception as exc:
                log.warning("Could not warn chat %s: %s", chat_id, exc)
            continue
        extra = [right for right in EXTRA_RIGHTS if getattr(member, right, False)]
        if extra:
            log.warning(
                "Bot has unnecessary admin rights in chat %s: %s. They can be turned off.",
                chat_id,
                ", ".join(extra),
            )
        else:
            log.info("Bot is an administrator in chat %s with minimal rights", chat_id)


async def register_commands(bot: Bot, settings: Settings) -> None:
    """Replace global and chat-specific menus, including earlier command overrides."""
    scopes = [
        BotCommandScopeDefault(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllPrivateChats(),
    ]
    for scope in scopes:
        await bot.set_my_commands([], scope=scope)
    for chat_id in sorted(settings.allowed_chat_ids):
        if settings.is_chat_allowed(chat_id):
            await bot.set_my_commands(bot_commands(), scope=BotCommandScopeChat(chat_id=chat_id))


async def run(settings: Settings) -> None:
    if settings.setup_mode:
        log.warning(
            "ALLOWED_CHAT_IDS is empty: all commands and notifications are disabled. "
            "Configure one negative group id and restart the bot."
        )
    if not settings.bot_admin_ids:
        log.warning(
            "BOT_ADMIN_IDS is empty: nobody can run admin actions. Use /me in the configured group."
        )
    if not settings.github_token:
        log.warning("GITHUB_TOKEN is empty: GitHub features are disabled")

    db = Database(settings.database_path)
    await db.connect()
    try:
        await _run_bot(settings, Storage(db, settings.default_timezone))
    finally:
        # Always close the DB: aiosqlite's worker thread would otherwise keep the process alive.
        await db.close()
        log.info("Bot stopped")


async def _run_bot(settings: Settings, storage: Storage) -> None:
    await storage.workflow.clear_unfinished_forms()
    await apply_known_migrations(settings, storage)
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview=LinkPreviewOptions(is_disabled=True),
            # Answer even if the command message was deleted meanwhile.
            allow_sending_without_reply=True,
        ),
    )
    ctx = build_context(settings, bot, storage)

    async def on_migrate(old_chat_id: int, new_chat_id: int) -> None:
        await storage.chats.migrate_chat(old_chat_id, new_chat_id)
        ctx.directory.invalidate_chat(old_chat_id)
        log.warning(
            "!!! Chat %s was migrated to supergroup %s. Data moved. UPDATE ALLOWED_CHAT_IDS in "
            ".env: replace %s with %s !!!",
            old_chat_id,
            new_chat_id,
            old_chat_id,
            new_chat_id,
        )

    dp = build_dispatcher(ctx, on_migrate)
    scheduler = create_scheduler(
        settings, ctx.archive, ctx.reminders, ctx.tasks, ctx.hack_notifications
    )

    try:
        await wait_for_telegram(bot)
        try:
            await register_commands(bot, settings)
            legacy_private_chats = await storage.db.fetchall(
                "SELECT DISTINCT chat_id FROM hackathons WHERE chat_id>0 "
                "UNION SELECT DISTINCT chat_id FROM hack_sessions WHERE chat_id>0"
            )
            for row in legacy_private_chats:
                await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=row["chat_id"]))
        except Exception as exc:
            log.warning("set_my_commands failed: %s", exc)
        if not settings.setup_mode:
            try:
                await check_bot_rights(bot, settings)
            except Exception as exc:
                log.warning("Startup rights check failed: %s", exc)
            scheduler.start()
        log.info("Bot started (long polling)")
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_signals=True)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await ctx.github.close()
        await ctx.hacks.close()
        await bot.session.close()
