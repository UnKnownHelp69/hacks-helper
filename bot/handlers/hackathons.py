"""The /hack entry point and owner-bound interactive hackathon workflows."""

from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.context import AppContext
from bot.db.database import utcnow
from bot.db.models import HACK_ACTIVE, HACK_FINISHED, HackSession
from bot.errors import UserError
from bot.handlers.base import usage
from bot.handlers.import_progress import ImportProgress
from bot.handlers.render import hackathon_card
from bot.middlewares.security import command_name
from bot.respond import edit_personal_reply, personal_options, personal_reply
from bot.services.args import parse_command
from bot.services.hack_data import (
    DATE_FIELDS,
    FIELD_LABELS,
    REQUIRED_FIELDS,
    input_field,
    parse_checkpoint,
    validate_hack,
)
from bot.services.timeutil import format_dt

router = Router(name="hackathons")
Event = Message | CallbackQuery


def button(
    session: HackSession, text: str, action: str, argument: str | int = ""
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=f"hk:{session.token}:{session.revision}:{action}:{argument}"
    )


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def menu(event: Event, ctx: AppContext, session: HackSession | None = None) -> None:
    message = event.message if isinstance(event, CallbackQuery) else event
    if session is None:
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
    drafts = await ctx.storage.workflow.drafts(session.user_id, session.chat_id)
    if drafts:
        rows.append([button(session, f"📝 Мои черновики ({len(drafts)})", "drafts", 0)])
    title = "Архив хакатонов" if archived else "Хакатоны"
    text = f"🏆 <b>{title}</b>\n" + (
        "Выберите хакатон или создайте новый."
        if hacks
        else "Активных хакатонов нет"
        if not archived
        else "Архив пуст"
    )
    await personal_reply(event, text, reply_markup=keyboard(rows))


async def preview(
    event: Event,
    ctx: AppContext,
    session: HackSession,
    error: str | None = None,
    *,
    target: Message | None = None,
) -> None:
    tz = await ctx.zone(session.chat_id)
    lines = ["📝 <b>Проверьте данные хакатона</b>"]
    if session.data.get("llm_imported"):
        lines.append("Данные дополнены LLM. Проверьте их перед публикацией.")
    for key, label in FIELD_LABELS.items():
        value = session.data.get(key)
        if key == "spec_url" and session.data.get("spec_file_id"):
            value = session.data.get("spec_file_name") or "файл"
        if key in DATE_FIELDS and value:
            value = format_dt(datetime.fromisoformat(value), tz)
        lines.append(
            f"<b>{label}:</b> {escape(str(value)[:160]) if value is not None else 'не заполнено'}"
        )
    lines.append(f"Часовой пояс: {escape(tz.key)}")
    checkpoints = session.data.get("checkpoints", [])
    lines.append(f"Чекпоинты: {len(checkpoints)}")
    if session.data.get("import_sources"):
        lines.append(f"Изучено источников: {len(session.data['import_sources'])}")
    valid = True
    try:
        validate_hack(session.data)
    except UserError as exc:
        lines.append(f"\n⚠️ {exc.message}")
        valid = False
    if session.data.get("import_error"):
        lines.append(f"\nℹ️ {session.data['import_error']}")
    if error:
        lines.append(f"\n❌ {error}")
    fields = list(FIELD_LABELS.items())
    rows = [
        [button(session, label, "field", key) for key, label in fields[index : index + 2]]
        for index in range(0, len(fields), 2)
    ]
    rows.append([button(session, f"📍 Чекпоинты ({len(checkpoints)})", "checkpoints")])
    if valid:
        label = (
            "✅ Сохранить изменения"
            if session.data.get("editing_id")
            else "✅ Опубликовать и начать голосование"
        )
        rows.append([button(session, label, "publish")])
    rows.append([button(session, "В меню", "menu"), button(session, "Отменить черновик", "cancel")])
    if target is not None and isinstance(event, Message):
        try:
            await edit_personal_reply(event, target, "\n".join(lines), reply_markup=keyboard(rows))
        except TelegramAPIError:
            # An expired progress panel may be replaced only with another personal message.
            await personal_reply(event, "\n".join(lines), reply_markup=keyboard(rows), edit=False)
    else:
        await personal_reply(event, "\n".join(lines), reply_markup=keyboard(rows))


async def import_message(
    message: Message, ctx: AppContext, raw: str, *, explicit: bool = False
) -> HackSession | None:
    async with ImportProgress(message, await ctx.zone(message.chat.id)) as progress:
        imported = await ctx.hacks.import_post(
            message.chat.id,
            message.from_user.id,
            raw,
            explicit=explicit,
            on_progress=progress.update,
        )
    if imported is not None:
        await preview(message, ctx, imported, target=progress.panel)
    elif progress.panel is not None:
        await edit_personal_reply(message, progress.panel, "Хакатон в сообщении не найден")
    return imported


async def prompt(
    event: Event, ctx: AppContext, session: HackSession, field: str, error: str | None = None
) -> None:
    session.step = f"input:{field}"
    session.data.pop("_prompt_id", None)
    await ctx.storage.workflow.save_session(session)
    if field == "import":
        text = (
            "Пришлите ссылку на хакатон или текст поста. "
            "Сначала вы проверите данные, затем подтвердите публикацию."
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
    text += "\nОтветьте на это личное сообщение. «Отмена» — вернуться к черновику."
    sent = await personal_reply(
        event,
        text,
        reply_markup=ForceReply(
            selective=False, input_field_placeholder="Введите значение или «Отмена»"
        ),
        edit=False,
    )
    if isinstance(sent, Message):
        session.data["_prompt_id"] = (
            sent.ephemeral_message_id if sent.ephemeral_message_id is not None else sent.message_id
        )
        await ctx.storage.workflow.save_session(session)


async def show_hack(event: Event, ctx: AppContext, session: HackSession, notice: str = "") -> None:
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    text = await hackathon_card(ctx, hack, utcnow())
    votes = await ctx.storage.workflow.votes(hack.id)
    mine = next((choice for choice, users in votes.items() if session.user_id in users), None)
    labels = {"yes": "участвую", "no": "не участвую", "maybe": "пока думаю"}
    text += (
        f"\nРазмер команды: {hack.team_min}–{hack.team_max}"
        f"\nМой голос: {labels.get(mine, 'не выбран')}"
    )
    checkpoints = await ctx.storage.workflow.checkpoints(hack.id)
    tz = await ctx.zone(hack.chat_id)
    for checkpoint in checkpoints:
        text += f"\n📍 {escape(checkpoint.title)}: {format_dt(checkpoint.at, tz)}"
    rows = []
    if hack.is_active and hack.end_at > utcnow():
        rows.append(
            [
                button(session, "✅ Участвую", "vote", "yes"),
                button(session, "❌ Нет", "vote", "no"),
                button(session, "🤔 Думаю", "vote", "maybe"),
            ]
        )
        rows.append([button(session, "✏️ Редактировать", "edit")])
    rows.append([button(session, "👥 Команды", "teams", 0), button(session, "Голоса", "votes", 0)])
    if hack.spec_file_id:
        rows.append([button(session, "📄 Получить ТЗ", "spec")])
    rows.append([button(session, "В меню", "menu")])
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


async def show_votes(event: Event, ctx: AppContext, session: HackSession, page: int) -> None:
    hack = await ctx.hacks.hackathon(int(session.data["hack_id"]), session.user_id)
    votes = await ctx.storage.workflow.votes(hack.id)
    entries = [(choice, uid) for choice, users in votes.items() for uid in users]
    page = min(max(page, 0), max(0, (len(entries) - 1) // 20))
    labels = {"yes": "✅", "no": "❌", "maybe": "🤔"}
    lines = [f"<b>Голоса: {escape(hack.display_name)}</b>"]
    for choice, uid in entries[page * 20 : (page + 1) * 20]:
        lines.append(f"{labels[choice]} {await ctx.directory.plain(hack.chat_id, uid)}")
    rows = []
    if page:
        rows.append([button(session, "←", "votes", page - 1)])
    if (page + 1) * 20 < len(entries):
        rows.append([button(session, "→", "votes", page + 1)])
    rows.append([button(session, "К карточке", "card")])
    await personal_reply(event, "\n".join(lines), reply_markup=keyboard(rows))


async def checkpoints_menu(event: Event, ctx: AppContext, session: HackSession) -> None:
    tz = await ctx.zone(session.chat_id)
    lines = ["📍 <b>Чекпоинты</b>"]
    rows = []
    for index, checkpoint in enumerate(session.data.get("checkpoints", [])):
        at = format_dt(datetime.fromisoformat(checkpoint["at"]), tz)
        lines.append(f"• {escape(checkpoint['title'])}: {at}")
        rows.append([button(session, f"Удалить: {checkpoint['title'][:35]}", "cpdel", index)])
    if len(session.data.get("checkpoints", [])) < 20:
        rows.append([button(session, "➕ Добавить чекпоинт", "cpadd")])
    rows.append([button(session, "К черновику", "preview")])
    await personal_reply(event, "\n".join(lines), reply_markup=keyboard(rows))


async def show_drafts(event: Event, ctx: AppContext, session: HackSession, page: int) -> None:
    drafts = await ctx.storage.workflow.drafts(session.user_id, session.chat_id)
    page = min(max(page, 0), max(0, (len(drafts) - 1) // 8))
    rows = [
        [
            button(
                session, str(draft.data.get("title") or "Новый хакатон")[:60], "resume", draft.token
            )
        ]
        for draft in drafts[page * 8 : (page + 1) * 8]
    ]
    if page:
        rows.append([button(session, "←", "drafts", page - 1)])
    if (page + 1) * 8 < len(drafts):
        rows.append([button(session, "→", "drafts", page + 1)])
    rows.append([button(session, "В меню", "menu")])
    await personal_reply(event, "📝 <b>Мои черновики</b>", reply_markup=keyboard(rows))


@router.message(Command("hack"))
async def cmd_hack(message: Message, ctx: AppContext) -> None:
    tokens = parse_command(message).tokens
    if len(tokens) > 1:
        raise usage("/hack [название]")
    await ctx.hacks.require_access(message.chat.id, message.from_user.id)
    if tokens:
        hack = await ctx.hackathon(message.chat.id, tokens[0].text)
        session = await ctx.storage.workflow.new_session(
            message.from_user.id, message.chat.id, "view", {"hack_id": hack.id}
        )
        await show_hack(message, ctx, session)
    else:
        await menu(message, ctx)


@router.callback_query(F.data.startswith("hk:"))
async def callback_hack(callback: CallbackQuery, ctx: AppContext) -> None:
    parts = (callback.data or "").split(":", 4)
    if len(parts) != 5 or not parts[2].isdigit():
        raise UserError("Кнопка устарела. Откройте /hack")
    _, token, revision, action, argument = parts
    session = await ctx.hacks.session(callback.from_user.id, token)
    if callback.message is None or callback.message.chat.id != session.chat_id:
        raise UserError("Эта кнопка относится к другому чату")
    if session.revision != int(revision):
        raise UserError("Кнопка устарела. Откройте /hack или продолжите последний черновик")
    if action != "publish":
        await ctx.storage.workflow.save_session(session)
    await callback.answer()
    try:
        if action == "menu":
            await menu(callback, ctx)
        elif action in {"page", "archive"}:
            session.data["page"] = int(argument) if action == "page" else 0
            if action == "archive":
                session.data["archive"] = not session.data.get("archive", False)
            await ctx.storage.workflow.save_session(session)
            await menu(callback, ctx, session)
        elif action in {"manual", "import"}:
            session = await ctx.storage.workflow.new_session(
                session.user_id,
                session.chat_id,
                "preview",
                {"checkpoints": [], "_wizard": action == "manual"},
            )
            await prompt(callback, ctx, session, "title" if action == "manual" else "import")
        elif action == "field":
            if argument not in FIELD_LABELS:
                raise UserError("Неизвестное поле")
            await prompt(callback, ctx, session, argument)
        elif action in {"preview", "checkpoints", "cpdel"}:
            if action == "cpdel":
                index = int(argument)
                if not 0 <= index < len(session.data.get("checkpoints", [])):
                    raise UserError("Чекпоинт уже удалён")
                session.data["checkpoints"].pop(index)
            session.step = "preview" if action == "preview" else "checkpoints"
            await ctx.storage.workflow.save_session(session)
            await (
                preview(callback, ctx, session)
                if action == "preview"
                else checkpoints_menu(callback, ctx, session)
            )
        elif action == "cpadd":
            await prompt(callback, ctx, session, "checkpoint")
        elif action == "cancel":
            session.step = "cancelled"
            await ctx.storage.workflow.save_session(session)
            await menu(callback, ctx)
        elif action == "drafts":
            await show_drafts(callback, ctx, session, int(argument))
        elif action == "resume":
            draft = await ctx.hacks.session(session.user_id, argument)
            if draft.chat_id != session.chat_id or draft.step in {"published", "cancelled", "menu"}:
                raise UserError("Черновик недоступен")
            draft.step = "preview"
            await ctx.storage.workflow.save_session(draft)
            await preview(callback, ctx, draft)
        elif action == "publish":
            hack_id = await ctx.hacks.publish(session)
            session = await ctx.storage.workflow.new_session(
                session.user_id, session.chat_id, "view", {"hack_id": hack_id}
            )
            await show_hack(callback, ctx, session, "✅ Сохранено. Общая карточка появится в чате.")
            await ctx.hack_notifications.flush()
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
            await preview(callback, ctx, session)
        elif action == "vote":
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
            await show_votes(callback, ctx, session, int(argument))
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
        if session.step == "preview":
            await preview(callback, ctx, session, exc.message)
        else:
            await personal_reply(
                callback,
                f"❌ {exc.message}",
                reply_markup=keyboard([[button(session, "В меню", "menu")]]),
            )


@router.callback_query(F.data.startswith("hv:") | F.data.startswith("hm:"))
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
    result = None
    if parts[0] == "hv":
        if len(parts) != 3:
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
    if session and session.step.startswith("input:") and private_input:
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
            session.step = "preview"
            await ctx.storage.workflow.save_session(session)
            await preview(message, ctx, session)
            return
        try:
            tz = await ctx.zone(session.chat_id)
            if field == "import":
                imported = await import_message(message, ctx, raw, explicit=True)
                if imported is None:
                    raise UserError(
                        "Хакатон не распознан. Пришлите пост с описанием или ссылку на его страницу"
                    )
                session.step = "cancelled"
                await ctx.storage.workflow.save_session(session)
                return
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
            session.step = "preview"
            await ctx.storage.workflow.save_session(session)
            missing = next((key for key in REQUIRED_FIELDS if session.data.get(key) is None), None)
            if session.data.get("_wizard") and missing:
                await prompt(message, ctx, session, missing)
            else:
                await preview(message, ctx, session)
        except UserError as exc:
            await prompt(message, ctx, session, field, exc.message)
        return
    raw = message.text or message.caption or ""
    for entity in [*(message.entities or []), *(message.caption_entities or [])]:
        if entity.type == "text_link" and entity.url:
            raw += "\n" + entity.url
    await import_message(message, ctx, raw)
