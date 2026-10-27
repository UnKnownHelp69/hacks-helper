"""Buttons that show answers in a popup visible only to the presser."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot.context import AppContext
from bot.handlers.views import PERSONAL_VIEWS
from bot.respond import fits_popup, html_to_plain, personal_reply

router = Router(name="popups")

EXPIRED = "Ответ устарел, повторите команду"
POSTED_TO_CHAT = "Ответ показан личным сообщением"


@router.callback_query(F.data.startswith("pop:"))
async def cb_popup(callback: CallbackQuery, ctx: AppContext) -> None:
    assert callback.data is not None
    message = callback.message
    raw_id = callback.data.split(":", 1)[1]
    text = None
    if message is not None and raw_id.isdigit():
        text = await ctx.storage.popups.get(int(raw_id), message.chat.id)
    await callback.answer(text or EXPIRED, show_alert=True)


@router.callback_query(F.data.startswith("my:"))
async def cb_personal(callback: CallbackQuery, ctx: AppContext) -> None:
    assert callback.data is not None
    view = PERSONAL_VIEWS.get(callback.data.split(":", 1)[1])
    message = callback.message
    if view is None or not isinstance(message, Message):
        await callback.answer(EXPIRED, show_alert=True)
        return
    text = await view(ctx, message.chat.id, callback.from_user)
    if fits_popup(text):
        await callback.answer(html_to_plain(text), show_alert=True)
        return
    await callback.answer(POSTED_TO_CHAT)
    await personal_reply(callback, text)
