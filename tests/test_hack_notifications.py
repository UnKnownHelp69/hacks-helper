from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from bot.services.hack_notifications import HackNotifications
from bot.services.notify import Messenger
from bot.services.users import UserDirectory
from tests.conftest import FakeBot, make_settings
from tests.test_hack_workflow import draft


class CardBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.fail = False
        self.deleted = False
        self.edits = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise OSError("Telegram is offline")
        await super().send_message(chat_id, text, **kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, **kwargs):
        if self.deleted:
            raise TelegramBadRequest(
                method=EditMessageText(**kwargs), message="message to edit not found"
            )
        self.edits.append(kwargs)
        return True


async def test_public_cards_and_team_notices_retry_and_then_deduplicate(storage):
    session, fields = await draft(storage)
    hack_id = await storage.workflow.save_hack(session, fields)
    api = CardBot()
    notifications = HackNotifications(
        make_settings(), storage, api, UserDirectory(api, storage.users), Messenger(api)
    )
    api.fail = True
    await notifications.flush()
    assert len(await storage.workflow.dirty_publications()) == 1
    await storage.workflow.vote(hack_id, 1, "yes")
    await storage.workflow.vote(hack_id, 2, "yes")
    api.fail = False
    await notifications.flush()
    assert len(api.sent) == 2
    assert await storage.workflow.dirty_publications() == []
    assert await storage.workflow.pending_notices() == []
    await notifications.flush()
    assert len(api.sent) == 2
    await storage.workflow.vote(hack_id, 3, "yes")
    await notifications.flush()
    assert len(api.edits) == 1
    api.deleted = True
    await storage.workflow.vote(hack_id, 4, "yes")
    await notifications.flush()
    assert len(api.sent) == 3
    assert await storage.workflow.dirty_publications() == []
