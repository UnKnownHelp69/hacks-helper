"""The /hack entry point and owner-bound interactive hackathon workflows."""

from __future__ import annotations

from datetime import datetime
from functools import partial
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    Message,
)

from bot.context import AppContext
from bot.db.database import utcnow
from bot.db.models import HACK_ACTIVE, HACK_FINISHED, HackSession
from bot.errors import UserError
from bot.handlers.base import usage
from bot.handlers.import_progress import ImportProgress
from bot.handlers.interactive import callback_session, keyboard, session_button
from bot.middlewares.security import command_name
from bot.respond import personal_options, personal_reply
from bot.services.args import parse_command
from bot.services.hack_data import (
    DATE_FIELDS,
    FIELD_LABELS,
    REQUIRED_FIELDS,
    import_date_issues,
    input_field,
    parse_checkpoint,
    validate_hack,
)
from bot.services.hack_import import HACK_RE, URL_RE
from bot.services.timeutil import format_dt

router = Router(name="hackathons")
Event = Message | CallbackQuery


button = partial(session_button, "hk")


async def menu(event: Event, ctx: AppContext, session: HackSession | None = None) -> None:
    message = event.message if isinstance(event, CallbackQuery) else event
    if session is None:
        await ctx.storage.workflow.clear_forms(event.from_user.id, message.chat.id)
        await ctx.hacks.require_access(message.chat.id, event.from_user.id)
        session = await ctx.storage.workflow.new_session(event.from_user.id, message.chat.id)
    archived = session.data.get("archive", False)
    hacks = await ctx.storage.hackathons.list(
        session.chat_id, HACK_FINISHED if archived else HACK_ACTIVE
    )
    page = min(max(int(session.data.get("page", 0)), 0), max(0, (len(hacks) - 1) // 8))
    rows = [
        [button(session, hack.display_name[:60], "open", hack.id)]
        for hack in hacks[page * 8 : (page + 1) * 8]
    ]
    pages = []
    if page:
        pages.append(button(session, "←", "page", page - 1))
    if (page + 1) * 8 < len(hacks):
        pages.append(button(session, "→", "page", page + 1))
    if pages:
        rows.append(pages)
    rows.extend(
        [
            [
                button(session, "➕ Создать вручную", "manual"),
                button(session, "🔗 Из поста / ссылки", "import"),
            ],
            [button(session, "Активные" if archived else "Архив", "archive")],
        ]
    )
    title = "Архив хакатонов" if archived else "Хакатоны"
    text = f"🏆 <b>{title}</b>\n" + (
        "Выберите хакатон или создайте новый."
        if hacks
        else "Активных хакатонов нет"
        if not archived
        else "Архив пуст"
    )
    await personal_reply(event, text, reply_markup=keyboard(rows))


async def edit_menu(event: Event, ctx: AppContext, session: HackSession) -> None:
    if not session.data.get("editing_id"):
        raise UserError("Откройте существующий хакатон через /hack")
    tz = await ctx.zone(session.chat_id)
    lines = ["✏️ <b>Настройки хакатона</b>", "Изменения сохраняются сразу."]
    for key, label in FIELD_LABELS.items():
        value = session.data.get(key)
        if key in DATE_FIELDS and value:
            value = format_dt(datetime.fromisoformat(value), tz)
        lines.append(
            f"<b>{label}:</b> {escape(str(value)[:160]) if value is not None else 'не задано'}"
        )
    fields = list(FIELD_LABELS.items())
    rows = [
        [button(session, label, "field", key) for key, label in fields[index : index + 2]]
        for index in range(0, len(fields), 2)
    ]
    rows.append([button(session, "Чекпоинты", "checkpoints")])
    rows.append([button(session, "Назад", "open", session.data["editing_id"])])
    await personal_reply(event, "\n".join(lines), reply_markup=keyboard(rows))


async def saved_edit(ctx: AppContext, session: HackSession) -> HackSession:
    hack_id = await ctx.hacks.publish(session)
    await ctx.storage.workflow.delete_session(session)
    await ctx.hack_notifications.flush()
    return await ctx.hacks.edit(hack_id, session.user_id)


async def creation_result(event: Event, ctx: AppContext, hack_id: int) -> None:
    message = event.message if isinstance(event, CallbackQuery) else event
    hack = await ctx.hacks.hackathon(hack_id, event.from_user.id)
    session = await ctx.storage.workflow.new_session(
        event.from_user.id, message.chat.id, "view", {"hack_id": hack_id}
    )
    await personal_reply(
        event,
        f"✅ Хакатон <b>{escape(hack.display_name)}</b> создан. Голосование открыто.",
        reply_markup=keyboard([[button(session, "Открыть хакатон", "open", hack_id)]]),
    )
    await ctx.hack_notifications.flush()


async def import_message(
    message: Message, ctx: AppContext, raw: str, *, explicit: bool = False
) -> bool:
    if not explicit and not HACK_RE.search(raw) and not URL_RE.search(raw):
        return False
    await ctx.storage.workflow.clear_forms(message.from_user.id, message.chat.id)
    try:
        async with ImportProgress(message, await ctx.zone(message.chat.id)) as progress:
            hack = await ctx.hacks.import_post(
                message.chat.id,
                message.from_user.id,
                raw,
                explicit=explicit,
                on_progress=progress.update,
            )
        if hack is not None:
            await creation_result(message, ctx, hack.id)
            return True
        if progress.panel is not None:
            await personal_reply(message, "Хакатон не создан: описание хакатона не найдено.")
    except UserError as exc:
        await personal_reply(message, f"❌ {escape(exc.message)}")
    return False


async def prompt(
    event: Event, ctx: AppContext, session: HackSession, field: str, error: str | None = None
) -> None:
    session.step = f"form:{field}"
    session.data.pop("_prompt_id", None)
    await ctx.storage.workflow.save_session(session)
    if field == "import":
        text = (
            "Пришлите сайт хакатона или прямую ссылку на положение / регламент. "
            "Бот найдёт документы и заполнит данные из них. "
            "Если все обязательные данные найдены, хакатон создастся автоматически."
        )
    elif field == "checkpoint":
        text = "Введите чекпоинт: Название | ДД.ММ.ГГГГ ЧЧ:ММ"
    else:
        text = f"Введите: <b>{FIELD_LABELS[field]}</b>."
        if field in DATE_FIELDS:
            tz = await ctx.zone(session.chat_id)
            text += f"\nФормат: ДД.ММ.ГГГГ ЧЧ:ММ. Часовой пояс: {escape(tz.key)}."
        elif field in {"team_min", "team_max"}:
            text += "\nЦелое число от 1 до 100."
        elif field == "repo":
            text += "\nФормат: owner/repo или ссылка на GitHub."
        elif field == "spec_url":
            text += "\nСсылка или документ."
        if field not in REQUIRED_FIELDS:
            text += "\nЗнак «-» — очистить поле."
    if error:
        text = f"❌ {error}\n\n" + text
    text += "\nОтветьте на это личное сообщение. «Отмена» — прекратить ввод."
    sent = await personal_reply(
        event,
        text,
        reply_markup=keyboard([[button(session, "Отмена", "cancel")]]),
    )
    if isinstance(sent, Message):
        session.data["_prompt_id"] = (
            sent.ephemeral_message_id if sent.ephemeral_message_id is not None else sent.message_id
        )
        await ctx.storage.workflow.save_session(session)


async def show_hack(event: Event, ctx: AppContext, session: HackSession, notice: str = "") -> None:
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    text, _ = await ctx.hack_notifications.card(hack)
    checkpoints = await ctx.storage.workflow.checkpoints(hack.id)
    rows = []
    if hack.is_active and hack.end_at > utcnow():
        rows.append(
            [
                button(session, "✅ Участвую", "vote", "yes"),
                button(session, "❌ Нет", "vote", "no"),
            ]
        )
    details = [button(session, "👥 Команды", "teams", 0)]
    if checkpoints:
        details.append(button(session, "📍 Чекпоинты", "checkpoints"))
    rows.append(details)
    if hack.spec_file_id:
        rows.append([button(session, "📄 Получить ТЗ", "spec")])
    navigation = []
    if hack.is_active and hack.end_at > utcnow():
        navigation.append(button(session, "✏️ Редактировать", "edit"))
    navigation.append(button(session, "В меню", "menu"))
    rows.append(navigation)
    if notice:
        text = notice + "\n\n" + text
    await personal_reply(event, text, reply_markup=keyboard(rows))


async def show_teams(event: Event, ctx: AppContext, session: HackSession, page: int = 0) -> None:
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    teams = await ctx.storage.workflow.teams(hack.id)
    votes = await ctx.storage.workflow.votes(hack.id)
    mine = next((team for team in teams if session.user_id in team.members), None)
    page = min(max(page, 0), max(0, (len(teams) - 1) // 8))
    lines = [
        f"👥 <b>{escape(hack.display_name)}</b>",
        f"Минимум: {hack.team_min}, максимум: {hack.team_max}",
    ]
    lines.append(f"Моя команда: {escape(mine.name) if mine else 'не выбрана'}")
    rows = []
    can_join = (
        session.user_id in votes["yes"] and hack.is_active and hack.registration_end > utcnow()
    )
    for team in teams[page * 8 : (page + 1) * 8]:
        state = "готова" if len(team.members) >= hack.team_min else "набор"
        lines.append(f"{escape(team.name)}: {len(team.members)}/{hack.team_max}, {state}")
        row = [button(session, f"Состав: {team.name}", "roster", f"{team.id}-0")]
        if can_join and (mine is None or mine.id != team.id):
            row.append(
                button(
                    session,
                    "Вступить" if len(team.members) < hack.team_max else "Сверх лимита",
                    "join",
                    team.id,
                )
            )
        rows.append(row)
    if not teams:
        lines.append("Команд пока нет. Выберите «Участвую», чтобы начать набор.")
    pages = []
    if page:
        pages.append(button(session, "←", "teams", page - 1))
    if (page + 1) * 8 < len(teams):
        pages.append(button(session, "→", "teams", page + 1))
    if pages:
        rows.append(pages)
    if can_join:
        rows.append([button(session, "➕ Новая команда", "newteam")])
    elif hack.is_active:
        rows.append([button(session, "✅ Участвую", "vote", "yes")])
    rows.append([button(session, "К карточке", "card"), button(session, "В меню", "menu")])
    await personal_reply(event, "\n".join(lines), reply_markup=keyboard(rows))


async def show_roster(event: Event, ctx: AppContext, session: HackSession, argument: str) -> None:
    team_id, page = map(int, argument.split("-", 1))
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    team = next(
        (team for team in await ctx.storage.workflow.teams(hack.id) if team.id == team_id), None
    )
    if team is None:
        raise UserError("Команда недоступна")
    page = min(max(page, 0), max(0, (len(team.members) - 1) // 20))
    lines = [f"👥 <b>{escape(team.name)}</b>: {len(team.members)}/{hack.team_max}"]
    for user_id in team.members[page * 20 : (page + 1) * 20]:
        lines.append("• " + await ctx.directory.plain(hack.chat_id, user_id))
    rows = []
    if page:
        rows.append([button(session, "←", "roster", f"{team.id}-{page - 1}")])
    if (page + 1) * 20 < len(team.members):
        rows.append([button(session, "→", "roster", f"{team.id}-{page + 1}")])
    rows.append([button(session, "К командам", "teams", 0)])
    await personal_reply(event, "\n".join(lines), reply_markup=keyboard(rows))


async def checkpoints_menu(event: Event, ctx: AppContext, session: HackSession) -> None:
    editing = bool(session.data.get("editing_id"))
    hack_id = int(session.data["editing_id"] if editing else session.data["hack_id"])
    await ctx.hacks.hackathon(hack_id, session.user_id)
    tz = await ctx.zone(session.chat_id)
    lines = ["📍 <b>Чекпоинты</b>"]
    rows = []
    entries = (
        [
            (item["title"], datetime.fromisoformat(item["at"]))
            for item in session.data.get("checkpoints", [])
        ]
        if editing
        else [(item.title, item.at) for item in await ctx.storage.workflow.checkpoints(hack_id)]
    )
    for index, (title, at) in enumerate(entries):
        lines.append(f"• {escape(title)}: {format_dt(at, tz)}")
        if editing:
            rows.append([button(session, f"Удалить: {title[:35]}", "cpdel", index)])
    if not entries:
        lines.append("Чекпоинтов пока нет.")
    if editing and len(entries) < 20:
        rows.append([button(session, "➕ Добавить чекпоинт", "cpadd")])
    rows.append([button(session, "Назад", "edit_menu" if editing else "card")])
    await personal_reply(event, "\n".join(lines), reply_markup=keyboard(rows))


@router.message(Command("hack"))
async def cmd_hack(message: Message, ctx: AppContext) -> None:
    tokens = parse_command(message).tokens
    if len(tokens) > 1:
        raise usage("/hack [название]")
    await ctx.hacks.require_access(message.chat.id, message.from_user.id)
    if tokens:
        await ctx.storage.workflow.clear_forms(message.from_user.id, message.chat.id)
        hack = await ctx.hackathon(message.chat.id, tokens[0].text)
        session = await ctx.storage.workflow.new_session(
            message.from_user.id, message.chat.id, "view", {"hack_id": hack.id}
        )
        await show_hack(message, ctx, session)
    else:
        await menu(message, ctx)


@router.callback_query(F.data.startswith("hk:"))
async def callback_hack(callback: CallbackQuery, ctx: AppContext) -> None:
    session, action, argument = await callback_session(callback, ctx, "/hack")
    if action != "publish":
        await ctx.storage.workflow.save_session(session)
    await callback.answer()
    try:
        if action == "menu":
            await menu(callback, ctx)
        elif action in {"llm", "llm_provider", "llm_page", "llm_pick", "llm_reset"}:
            await personal_reply(callback, "Личные настройки перенесены в /me")
        elif action in {"page", "archive"}:
            session.data["page"] = int(argument) if action == "page" else 0
            if action == "archive":
                session.data["archive"] = not session.data.get("archive", False)
            await ctx.storage.workflow.save_session(session)
            await menu(callback, ctx, session)
        elif action in {"manual", "import"}:
            await ctx.storage.workflow.clear_forms(session.user_id, session.chat_id)
            session = await ctx.storage.workflow.new_session(
                session.user_id,
                session.chat_id,
                "form:title" if action == "manual" else "form:import",
                {"checkpoints": [], "_mode": "create" if action == "manual" else "import"},
            )
            await prompt(callback, ctx, session, "title" if action == "manual" else "import")
        elif action == "field":
            if argument not in FIELD_LABELS or not session.data.get("editing_id"):
                raise UserError("Откройте настройки существующего хакатона")
            await prompt(callback, ctx, session, argument)
        elif action == "edit_menu":
            await edit_menu(callback, ctx, session)
        elif action in {"checkpoints", "cpdel"}:
            if action == "cpdel" and not session.data.get("editing_id"):
                raise UserError("Сначала создайте хакатон")
            if action == "cpdel":
                index = int(argument)
                if not 0 <= index < len(session.data.get("checkpoints", [])):
                    raise UserError("Чекпоинт уже удалён")
                session.data["checkpoints"].pop(index)
                session = await saved_edit(ctx, session)
            session.step = "edit_checkpoints" if session.data.get("editing_id") else "view"
            await ctx.storage.workflow.save_session(session)
            await checkpoints_menu(callback, ctx, session)
        elif action == "cpadd":
            if not session.data.get("editing_id"):
                raise UserError("Сначала создайте хакатон")
            await prompt(callback, ctx, session, "checkpoint")
        elif action == "cancel":
            hack_id = session.data.get("editing_id")
            await ctx.storage.workflow.delete_session(session)
            if hack_id:
                session = await ctx.storage.workflow.new_session(
                    callback.from_user.id, callback.message.chat.id, "view", {"hack_id": hack_id}
                )
                await show_hack(callback, ctx, session)
            else:
                await menu(callback, ctx)
        elif action in {"drafts", "resume", "preview", "publish"}:
            raise UserError("Черновиков больше нет. Откройте /hack, чтобы создать хакатон заново")
        elif action in {"open", "card"}:
            hack_id = int(argument) if action == "open" else int(session.data["hack_id"])
            hack = await ctx.hacks.hackathon(hack_id, session.user_id)
            if hack.chat_id != session.chat_id:
                raise UserError("Хакатон относится к другому чату")
            session.step, session.data = "view", {"hack_id": hack_id}
            await ctx.storage.workflow.save_session(session)
            await show_hack(callback, ctx, session)
        elif action == "edit":
            session = await ctx.hacks.edit(int(session.data["hack_id"]), session.user_id)
            await edit_menu(callback, ctx, session)
        elif action == "vote":
            if argument not in {"yes", "no"}:
                raise UserError("Выберите «Участвую» или «Нет»")
            result = await ctx.hacks.vote(int(session.data["hack_id"]), session.user_id, argument)
            if argument == "yes" and result is None:
                await show_teams(callback, ctx, session)
            else:
                await show_hack(callback, ctx, session, "✅ Голос учтён")
            await ctx.hack_notifications.flush()
        elif action in {"teams", "newteam", "join", "overflow"}:
            hack_id = int(session.data["hack_id"])
            if action in {"newteam", "overflow"}:
                await ctx.hacks.join_team(
                    hack_id,
                    session.user_id,
                    None if action == "newteam" else int(argument),
                    overflow=action == "overflow",
                )
            elif action == "join":
                hack = await ctx.hacks.hackathon(hack_id, session.user_id)
                team = next(
                    (
                        team
                        for team in await ctx.storage.workflow.teams(hack.id)
                        if team.id == int(argument)
                    ),
                    None,
                )
                if team is None:
                    raise UserError("Команда недоступна")
                if len(team.members) >= hack.team_max:
                    await personal_reply(
                        callback,
                        f"В {escape(team.name)} уже {len(team.members)} участников "
                        f"при максимуме {hack.team_max}. Присоединиться сверх лимита?",
                        reply_markup=keyboard(
                            [
                                [
                                    button(
                                        session, "Присоединиться сверх лимита", "overflow", team.id
                                    )
                                ],
                                [button(session, "К командам", "teams", 0)],
                            ]
                        ),
                    )
                    return
                await ctx.hacks.join_team(hack_id, session.user_id, team.id)
            await show_teams(callback, ctx, session, int(argument) if action == "teams" else 0)
            if action != "teams":
                await ctx.hack_notifications.flush()
        elif action == "roster":
            await show_roster(callback, ctx, session, argument)
        elif action == "votes":
            await show_hack(callback, ctx, session)
        elif action == "spec":
            hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
            if not hack.spec_file_id:
                raise UserError("Файл ТЗ не прикреплён")
            await callback.bot.send_document(
                **personal_options(callback), document=hack.spec_file_id
            )
            await show_hack(callback, ctx, session)
        else:
            raise UserError("Действие недоступно. Откройте /hack")
    except (ValueError, KeyError):
        raise UserError("Кнопка устарела. Откройте /hack") from None
    except UserError as exc:
        await personal_reply(
            callback,
            f"❌ {escape(exc.message)}",
            reply_markup=keyboard([[button(session, "В меню", "menu")]]),
        )


@router.callback_query(
    F.data.startswith("hv:") | F.data.startswith("hm:") | F.data.startswith("hc:")
)
async def public_hack_action(callback: CallbackQuery, ctx: AppContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) not in {2, 3} or not parts[1].isdigit():
        raise UserError("Кнопка недоступна")
    hack = await ctx.hacks.hackathon(int(parts[1]), callback.from_user.id)
    if callback.message is None or callback.message.chat.id != hack.chat_id:
        raise UserError("Хакатон относится к другому чату")
    await callback.answer()
    session = await ctx.storage.workflow.new_session(
        callback.from_user.id, hack.chat_id, "view", {"hack_id": hack.id}
    )
    if parts[0] == "hc":
        if len(parts) != 2:
            raise UserError("Кнопка недоступна")
        await checkpoints_menu(callback, ctx, session)
        return
    result = None
    if parts[0] == "hv":
        if len(parts) != 3 or parts[2] not in {"yes", "no"}:
            raise UserError("Неизвестный вариант голосования")
        result = await ctx.hacks.vote(hack.id, session.user_id, parts[2])
    if parts[0] == "hv" and parts[2] == "yes" and result is None:
        await show_teams(callback, ctx, session)
    else:
        await show_hack(callback, ctx, session, "✅ Голос учтён" if parts[0] == "hv" else "")
    if parts[0] == "hv":
        await ctx.hack_notifications.flush()


@router.message(F.text | F.caption | F.document, lambda message: command_name(message) is None)
async def hack_input(message: Message, ctx: AppContext) -> None:
    actor = message.from_user
    if actor is None or actor.is_bot:
        return
    session = await ctx.storage.workflow.session(actor.id, chat_id=message.chat.id)
    private_input = message.chat.type == "private" or message.ephemeral_message_id is not None
    if session and session.step.startswith("form:") and private_input:
        await ctx.hacks.require_access(session.chat_id, actor.id)
        field = session.step.split(":", 1)[1]
        expected = session.data.get("_prompt_id")
        replied = message.reply_to_message
        if expected is not None and replied is not None:
            actual = (
                replied.ephemeral_message_id
                if replied.ephemeral_message_id is not None
                else replied.message_id
            )
            if actual != expected:
                await personal_reply(
                    message, "Это ответ на старый запрос. Откройте нужное поле через /hack"
                )
                return
        raw = (message.text or message.caption or "").strip()
        if raw.lower() in {"отмена", "cancel"}:
            hack_id = session.data.get("editing_id")
            await ctx.storage.workflow.delete_session(session)
            if hack_id:
                session = await ctx.storage.workflow.new_session(
                    actor.id, message.chat.id, "view", {"hack_id": hack_id}
                )
                await show_hack(message, ctx, session)
            else:
                await menu(message, ctx)
            return
        if field == "import":
            await ctx.storage.workflow.delete_session(session)
            await import_message(message, ctx, raw, explicit=True)
            return
        try:
            tz = await ctx.zone(session.chat_id)
            if field == "checkpoint":
                if len(session.data.get("checkpoints", [])) >= 20:
                    raise UserError("Можно добавить до 20 чекпоинтов")
                session.data.setdefault("checkpoints", []).append(parse_checkpoint(raw, tz))
            elif field == "spec_url" and message.document:
                session.data.update(
                    spec_file_id=message.document.file_id,
                    spec_file_name=message.document.file_name,
                    spec_url=None,
                )
            else:
                session.data[field] = input_field(field, raw, tz)
                if field == "spec_url":
                    session.data.update(spec_file_id=None, spec_file_name=None)
            if session.data.get("editing_id"):
                updated = await saved_edit(ctx, session)
                await edit_menu(message, ctx, updated)
                return
            if session.data.get("_mode") != "create":
                raise UserError("Этот ввод устарел. Откройте /hack")
            if field in DATE_FIELDS and (issues := import_date_issues(session.data)):
                raise UserError(issues[0])
            missing = next((key for key in REQUIRED_FIELDS if session.data.get(key) is None), None)
            if missing:
                await prompt(message, ctx, session, missing)
                return
            validate_hack(session.data)
        except UserError as exc:
            await prompt(message, ctx, session, field, exc.message)
            return
        session.step = "completing"
        await ctx.storage.workflow.save_session(session)
        try:
            hack_id = await ctx.hacks.publish(session)
        except UserError as exc:
            await personal_reply(message, "❌ Хакатон не создан. " + escape(exc.message))
        else:
            await creation_result(message, ctx, hack_id)
        finally:
            await ctx.storage.workflow.delete_session(session)
        return
    raw = message.text or message.caption or ""
    for entity in [*(message.entities or []), *(message.caption_entities or [])]:
        if entity.type == "text_link" and entity.url:
            raw += "\n" + entity.url
    await import_message(message, ctx, raw)
