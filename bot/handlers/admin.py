"""Bot-admin only commands. Access is enforced centrally by AdminOnlyMiddleware."""

from __future__ import annotations

import logging
import re
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.context import AppContext
from bot.db.database import utcnow
from bot.errors import UserError
from bot.handlers.base import reply, usage, validate_slug
from bot.handlers.render import hackathon_card
from bot.middlewares.admin import AdminOnlyMiddleware
from bot.services.args import parse_command, reply_target, resolve_user_tokens
from bot.services.github import GitHubNotConfigured, normalize_repo
from bot.services.schedule_logic import MILESTONE_TITLES, parse_milestone, validate_dates
from bot.services.timeutil import format_dt, get_zone, parse_datetime, utc_offset_label

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_CLEAR_WORDS = {"-", "нет", "none", "удалить"}


@router.message(Command("hack_new"))
async def cmd_hack_new(message: Message, ctx: AppContext) -> None:
    cmd = parse_command(message)
    words = [t.text for t in cmd.tokens]
    if len(words) < 7:
        raise usage(
            "/hack_new <хак> <рег: ДД.ММ.ГГГГ ЧЧ:ММ> <начало: ДД.ММ.ГГГГ ЧЧ:ММ> "
            "<конец: ДД.ММ.ГГГГ ЧЧ:ММ> [название]"
        )
    slug = validate_slug(words[0])
    tz = await ctx.zone(message.chat.id)
    registration_end = parse_datetime(words[1], words[2], tz)
    start_at = parse_datetime(words[3], words[4], tz)
    end_at = parse_datetime(words[5], words[6], tz)
    validate_dates(registration_end, start_at, end_at)
    title = cmd.rest(7) or None
    if title and len(title) > 100:
        raise UserError("Слишком длинное название (максимум 100 символов)")
    if await ctx.storage.hackathons.get(message.chat.id, slug):
        raise UserError(f"Хакатон «{escape(slug)}» уже существует (возможно, в архиве)")
    hack = await ctx.storage.hackathons.create(
        message.chat.id, slug, title, registration_end, start_at, end_at
    )
    log.info("Hackathon %s created by %s", slug, message.from_user.id if message.from_user else "?")
    card = await hackathon_card(ctx, hack, utcnow())
    await reply(
        message,
        f"✅ Хакатон создан.\n\n{card}\n\nДальше: /repo {escape(slug)} owner/repo, "
        f"/add {escape(slug)} @участники",
    )


@router.message(Command("hack_date"))
async def cmd_hack_date(message: Message, ctx: AppContext) -> None:
    words = [t.text for t in parse_command(message).tokens]
    if len(words) != 4:
        raise usage("/hack_date <хак> reg|start|end <ДД.ММ.ГГГГ ЧЧ:ММ>")
    hack = await ctx.hackathon(message.chat.id, words[0])
    milestone = parse_milestone(words[1])
    tz = await ctx.zone(message.chat.id)
    value = parse_datetime(words[2], words[3], tz)
    dates = {
        "registration_end": hack.registration_end,
        "start": hack.start_at,
        "end": hack.end_at,
    }
    dates[milestone] = value
    validate_dates(dates["registration_end"], dates["start"], dates["end"])
    await ctx.storage.hackathons.update_dates(
        hack.id, dates["registration_end"], dates["start"], dates["end"]
    )
    note = "" if hack.is_active else "\nХакатон в архиве: дата исправлена только в истории."
    await message.reply(
        f"✅ {escape(hack.slug)}: {MILESTONE_TITLES[milestone]} — {format_dt(value, tz)}. "
        f"Напоминания пересчитаны.{note}"
    )


@router.message(Command("hack_title"))
async def cmd_hack_title(message: Message, ctx: AppContext) -> None:
    cmd = parse_command(message)
    if len(cmd.tokens) < 1:
        raise usage("/hack_title <хак> <название> (или «-», чтобы убрать)")
    hack = await ctx.hackathon(message.chat.id, cmd.tokens[0].text)
    title = cmd.rest(1)
    if not title or title in _CLEAR_WORDS:
        title = None
    elif len(title) > 100:
        raise UserError("Слишком длинное название (максимум 100 символов)")
    await ctx.storage.hackathons.update_fields(hack.id, title=title)
    await message.reply(f"✅ Название {escape(hack.slug)}: {escape(title or 'не задано')}")


@router.message(Command("hack_del"))
async def cmd_hack_del(message: Message, ctx: AppContext) -> None:
    words = [t.text for t in parse_command(message).tokens]
    if len(words) != 1:
        raise usage("/hack_del <хак>")
    hack = await ctx.hackathon(message.chat.id, words[0])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"hdel:{hack.id}:y"),
                InlineKeyboardButton(text="Отмена", callback_data=f"hdel:{hack.id}:n"),
            ]
        ]
    )
    await message.reply(
        f"Удалить хакатон <b>{escape(hack.display_name)}</b> со всеми участниками, задачами "
        "и статистикой? Это необратимо. (Завершённые хакатоны лучше оставить в архиве.)",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("hdel:"))
async def cb_hack_del(callback: CallbackQuery, ctx: AppContext) -> None:
    assert callback.data is not None
    _, raw_id, answer = callback.data.split(":", 2)
    message = callback.message
    hack = await ctx.storage.hackathons.get_by_id(int(raw_id))
    if message is None or hack is None or hack.chat_id != message.chat.id:
        await callback.answer("Хакатон уже удалён", show_alert=True)
        return
    if answer != "y":
        await callback.answer("Отменено")
        await message.edit_text("Удаление отменено")  # type: ignore[union-attr]
        return
    await ctx.storage.hackathons.delete(hack.id)
    log.info("Hackathon %s deleted by %s", hack.slug, callback.from_user.id)
    await callback.answer("Удалено")
    await message.edit_text(f"🗑 Хакатон {escape(hack.slug)} удалён")  # type: ignore[union-attr]


@router.message(Command("repo"))
async def cmd_repo(message: Message, ctx: AppContext) -> None:
    words = [t.text for t in parse_command(message).tokens]
    if len(words) != 2:
        raise usage("/repo <хак> owner/repo   (или «-», чтобы отвязать)")
    hack = await ctx.hackathon(message.chat.id, words[0])
    if words[1] in _CLEAR_WORDS:
        await ctx.storage.hackathons.update_fields(hack.id, repo=None)
        await message.reply(f"Репозиторий отвязан от {escape(hack.slug)}")
        return
    repo = normalize_repo(words[1])
    if not ctx.github.configured:
        raise GitHubNotConfigured("GitHub не настроен: не задан GITHUB_TOKEN")
    data = await ctx.github.get_repo(repo)
    repo = str(data.get("full_name") or repo)
    await ctx.storage.hackathons.update_fields(hack.id, repo=repo)
    await message.reply(
        f"✅ Репозиторий <code>{escape(repo)}</code> привязан к {escape(hack.slug)}. "
        f"Issues: /issues {escape(hack.slug)}"
    )


async def _member_targets(message: Message, ctx: AppContext) -> tuple[str, list[int], list[str]]:
    cmd = parse_command(message)
    if not cmd.tokens:
        raise usage("/add <хак> @user1 @user2 … (или ответом на сообщение: /add <хак>)")
    slug = cmd.tokens[0].text
    user_tokens = cmd.tokens[1:]
    assert message.from_user is not None
    if user_tokens:
        ids, errors = await resolve_user_tokens(
            user_tokens,
            chat_id=message.chat.id,
            caller_id=message.from_user.id,
            is_admin=True,
            directory=ctx.directory,
        )
        return slug, ids, errors
    target = reply_target(message)
    if target is None:
        raise UserError(
            "Укажите участников (@username, упоминание) или ответьте на сообщение человека"
        )
    await ctx.directory.remember(target)
    return slug, [target.id], []


@router.message(Command("add"))
async def cmd_add(message: Message, ctx: AppContext) -> None:
    slug, user_ids, errors = await _member_targets(message, ctx)
    hack = await ctx.hackathon(message.chat.id, slug)
    added, already = [], []
    for user_id in user_ids:
        name = await ctx.directory.plain(message.chat.id, user_id)
        if await ctx.storage.hackathons.add_member(hack.id, user_id):
            added.append(name)
        else:
            already.append(name)
    lines = []
    if added:
        lines.append(f"✅ Добавлены в {escape(hack.slug)}: {', '.join(added)}")
    if already:
        lines.append(f"Уже участвуют: {', '.join(already)}")
    lines.extend(f"⚠️ {err}" for err in errors)
    await reply(message, "\n".join(lines) or "Никто не добавлен")


@router.message(Command("remove"))
async def cmd_remove(message: Message, ctx: AppContext) -> None:
    slug, user_ids, errors = await _member_targets(message, ctx)
    hack = await ctx.hackathon(message.chat.id, slug)
    removed, missing = [], []
    for user_id in user_ids:
        name = await ctx.directory.plain(message.chat.id, user_id)
        if await ctx.storage.hackathons.remove_member(hack.id, user_id):
            await ctx.storage.tasks.delete_active_for_user(hack.id, user_id)
            removed.append(name)
        else:
            missing.append(name)
    lines = []
    if removed:
        lines.append(
            f"✅ Убраны из {escape(hack.slug)} (их активные задачи сняты): {', '.join(removed)}"
        )
    if missing:
        lines.append(f"Не участвовали: {', '.join(missing)}")
    lines.extend(f"⚠️ {err}" for err in errors)
    await reply(message, "\n".join(lines) or "Никто не убран")


@router.message(Command("place"))
async def cmd_place(message: Message, ctx: AppContext) -> None:
    words = [t.text for t in parse_command(message).tokens]
    if len(words) != 2:
        raise usage("/place <хак> <место> или /place <хак> нет")
    hack = await ctx.hackathon(message.chat.id, words[0])
    if hack.is_active:
        raise UserError("Место указывается после завершения хакатона")
    raw = words[1].lower()
    if raw in {"нет", "без", "0", "-", "none"}:
        place = 0
    elif raw.isdigit() and 0 < int(raw) < 10000:
        place = int(raw)
    else:
        raise UserError("Место — положительное число или «нет»")
    await ctx.storage.hackathons.update_fields(hack.id, place=place)
    text = "без места" if place == 0 else f"{place} место"
    await message.reply(f"✅ {escape(hack.slug)}: {text}")


async def _set_link(message: Message, ctx: AppContext, field_name: str, label: str) -> None:
    words = [t.text for t in parse_command(message).tokens]
    if len(words) != 2:
        raise usage(f"/{field_name} <хак> <ссылка> (или «-», чтобы удалить)")
    hack = await ctx.hackathon(message.chat.id, words[0])
    value = words[1]
    if value in _CLEAR_WORDS:
        await ctx.storage.hackathons.update_fields(hack.id, **{_LINK_FIELDS[field_name]: None})
        await message.reply(f"{label} для {escape(hack.slug)} удалена")
        return
    if not _URL_RE.match(value) or len(value) > 500:
        raise UserError("Нужна ссылка, начинающаяся с http:// или https://")
    await ctx.storage.hackathons.update_fields(hack.id, **{_LINK_FIELDS[field_name]: value})
    await message.reply(f"✅ {label} для {escape(hack.slug)} сохранена")


_LINK_FIELDS = {"site": "site_url", "orgchat": "org_chat_url"}


@router.message(Command("site"))
async def cmd_site(message: Message, ctx: AppContext) -> None:
    await _set_link(message, ctx, "site", "Ссылка на сайт")


@router.message(Command("orgchat"))
async def cmd_orgchat(message: Message, ctx: AppContext) -> None:
    await _set_link(message, ctx, "orgchat", "Ссылка на чат организаторов")


@router.message(Command("spec"))
async def cmd_spec(message: Message, ctx: AppContext) -> None:
    words = [t.text for t in parse_command(message).tokens]
    if not words:
        raise usage(
            "/spec <хак> <ссылка> | файл с подписью /spec <хак> | ответ /spec <хак> на файл"
        )
    hack = await ctx.hackathon(message.chat.id, words[0])
    document = message.document or (
        message.reply_to_message.document if message.reply_to_message else None
    )
    if len(words) == 1 and document is not None:
        await ctx.storage.hackathons.update_fields(
            hack.id,
            spec_file_id=document.file_id,
            spec_file_name=document.file_name,
            spec_url=None,
        )
        await message.reply(
            f"✅ ТЗ для {escape(hack.slug)}: файл «{escape(document.file_name or 'без имени')}». "
            f"Показать: /links {escape(hack.slug)}"
        )
        return
    if len(words) != 2:
        raise UserError(
            "Пришлите ссылку (/spec &lt;хак&gt; &lt;ссылка&gt;), отправьте файл с подписью "
            "/spec &lt;хак&gt; или ответьте /spec &lt;хак&gt; на сообщение с файлом"
        )
    value = words[1]
    if value in _CLEAR_WORDS:
        await ctx.storage.hackathons.update_fields(
            hack.id, spec_url=None, spec_file_id=None, spec_file_name=None
        )
        await message.reply(f"ТЗ для {escape(hack.slug)} удалено")
        return
    if not _URL_RE.match(value) or len(value) > 500:
        raise UserError("Нужна ссылка, начинающаяся с http:// или https://")
    await ctx.storage.hackathons.update_fields(
        hack.id, spec_url=value, spec_file_id=None, spec_file_name=None
    )
    await message.reply(f"✅ ТЗ для {escape(hack.slug)}: ссылка сохранена")


@router.message(Command("set_tz"))
async def cmd_set_tz(message: Message, ctx: AppContext) -> None:
    words = [t.text for t in parse_command(message).tokens]
    if len(words) != 1:
        raise usage("/set_tz <IANA-название>, например /set_tz Europe/Moscow")
    tz = get_zone(words[0])
    await ctx.storage.chats.set_timezone(message.chat.id, tz.key)
    now = utcnow()
    await message.reply(
        f"✅ Часовой пояс чата: <b>{escape(tz.key)}</b> ({utc_offset_label(tz, now)}), "
        f"сейчас {format_dt(now, tz)}.\n"
        "Даты хакатонов хранятся в UTC и не сдвигаются; напоминания и окна доступности "
        "теперь считаются в новом поясе."
    )
