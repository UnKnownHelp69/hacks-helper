"""General commands: /help and /me."""

from __future__ import annotations

from functools import partial
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.context import AppContext
from bot.db.models import HackSession
from bot.errors import UserError
from bot.handlers.interactive import callback_session, keyboard, session_button
from bot.handlers.views import me_view
from bot.respond import personal_reply, reply
from bot.texts import HELP_TEXT

router = Router(name="common")
Event = Message | CallbackQuery
button = partial(session_button, "me")


@router.message(Command("help"))
async def cmd_help(message: Message, ctx: AppContext) -> None:
    if ctx.settings.setup_mode:
        await reply(
            message,
            f"chat_id: <code>{message.chat.id}</code>\n"
            "Добавьте в ALLOWED_CHAT_IDS и перезапустите бота.",
        )
        return
    await reply(message, HELP_TEXT)


@router.message(Command("me"))
async def cmd_me(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    if not ctx.settings.is_chat_allowed(message.chat.id):
        # Before setup, reveal only the caller's own id, without reusable buttons.
        await reply(message, f"Ваш user_id: <code>{message.from_user.id}</code>")
        return
    await profile(message, ctx)


async def profile(event: Event, ctx: AppContext, session: HackSession | None = None) -> None:
    message = event.message if isinstance(event, CallbackQuery) else event
    if session is None:
        await ctx.hacks.require_access(message.chat.id, event.from_user.id)
        session = await ctx.storage.workflow.new_session(
            event.from_user.id, message.chat.id, "profile"
        )
    else:
        session.step = "profile"
        await ctx.storage.workflow.save_session(session)
    text = await me_view(ctx, session.chat_id, event.from_user)
    provider, model = await ctx.hacks.llm_selection(session.user_id)
    label = "GigaChat" if provider == "gigachat" else "OpenRouter / API"
    await personal_reply(
        event,
        "<b>Профиль и настройки</b>\n"
        + text
        + f"\nНейросеть: {label} · <code>{escape(model)}</code>",
        reply_markup=keyboard([[button(session, "🤖 Модель нейросети", "llm")]]),
    )


async def llm_menu(event: Event, ctx: AppContext, session: HackSession) -> None:
    session.step = "llm"
    await ctx.storage.workflow.save_session(session)
    provider, model = await ctx.hacks.llm_selection(session.user_id)
    label = "GigaChat" if provider == "gigachat" else "OpenRouter / совместимый API"
    text = (
        f"🤖 <b>Модель нейросети</b>\nСейчас: {label} · <code>{escape(model)}</code>\n\n"
        "Выбор сохраняется для вас и применяется к следующим импортам во всех подключённых чатах. "
        "Текущая обработка продолжится с прежней моделью.\n"
        "Подключение провайдеров настраивает владелец бота. "
        "Текст поста и страницы передаются выбранному провайдеру.\n"
        "💳 Платные модели расходуют баланс API; GigaChat — квоту вашего тарифа."
    )
    if not ctx.hacks.llm.is_configured(provider):
        text += "\n\n⚠️ Текущий провайдер не настроен: работает обычный парсер."
    await personal_reply(
        event,
        text,
        reply_markup=keyboard(
            [
                [button(session, "OpenRouter / совместимый API", "llm_provider", "openrouter")],
                [button(session, "GigaChat · Сбер", "llm_provider", "gigachat")],
                [button(session, "Сбросить на настройку бота", "llm_reset")],
                [button(session, "К профилю", "profile")],
            ]
        ),
    )


async def llm_models(
    event: Event, ctx: AppContext, session: HackSession, provider: str, page: int = 0
) -> None:
    if provider not in {"openrouter", "gigachat"}:
        raise UserError("Неизвестный провайдер")
    models = await ctx.hacks.llm.available_models(provider)
    session.step = "llm"
    session.data["_llm_provider"] = provider
    session.data["_llm_models"] = models
    await ctx.storage.workflow.save_session(session)
    selected = await ctx.hacks.llm_selection(session.user_id)
    page = min(max(page, 0), max(0, (len(models) - 1) // 8))
    rows = []
    for index in range(page * 8, min(len(models), (page + 1) * 8)):
        model = models[index]
        prefix = "✅ " if selected == (provider, model) else ""
        if provider == "openrouter" and ctx.hacks.llm.is_openrouter:
            prefix += "🆓 " if model == "openrouter/free" or model.endswith(":free") else "💳 "
        rows.append([button(session, (prefix + model)[:60], "llm_pick", index)])
    navigation = []
    if page:
        navigation.append(button(session, "←", "llm_page", page - 1))
    if (page + 1) * 8 < len(models):
        navigation.append(button(session, "→", "llm_page", page + 1))
    if navigation:
        rows.append(navigation)
    rows.append([button(session, "К выбору провайдера", "llm")])
    await personal_reply(
        event,
        f"🤖 <b>{'GigaChat' if provider == 'gigachat' else 'OpenRouter / API'}</b>\n"
        f"Страница {page + 1} из {max(1, (len(models) + 7) // 8)}. "
        "Выберите модель для следующих импортов.\n"
        "🆓 — бесплатная; 💳 — расходует баланс; GigaChat — по тарифу API.",
        reply_markup=keyboard(rows),
    )


@router.callback_query(F.data.startswith("me:"))
async def callback_me(callback: CallbackQuery, ctx: AppContext) -> None:
    session, action, argument = await callback_session(callback, ctx, "/me")
    await ctx.storage.workflow.save_session(session)
    await callback.answer()
    try:
        if action == "profile":
            await profile(callback, ctx, session)
        elif action == "llm":
            await llm_menu(callback, ctx, session)
        elif action == "llm_provider":
            await llm_models(callback, ctx, session, argument)
        elif action == "llm_page":
            await llm_models(callback, ctx, session, session.data["_llm_provider"], int(argument))
        elif action == "llm_pick":
            index = int(argument)
            models = session.data["_llm_models"]
            if session.step != "llm" or not 0 <= index < len(models):
                raise UserError("Список моделей устарел. Откройте /me")
            await ctx.hacks.select_llm(
                session.chat_id, session.user_id, session.data["_llm_provider"], models[index]
            )
            await llm_menu(callback, ctx, session)
        elif action == "llm_reset":
            await ctx.storage.users.reset_llm_preference(session.user_id)
            await llm_menu(callback, ctx, session)
        else:
            raise UserError("Действие недоступно. Откройте /me")
    except (ValueError, KeyError):
        raise UserError("Кнопка устарела. Откройте /me") from None
    except UserError as exc:
        await personal_reply(
            callback,
            f"❌ {exc.message}",
            reply_markup=keyboard([[button(session, "К профилю", "profile")]]),
        )
