import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from bot import app as app_module
from bot.config import ConfigError, load_settings


class FlakyBot:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return object()


async def test_wait_for_telegram_retries_network_errors(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)
    bot = FlakyBot([TelegramNetworkError(method=GetMe(), message="down")] * 3)
    await app_module.wait_for_telegram(bot)  # type: ignore[arg-type]
    assert bot.calls == 4
    assert sleeps == [5, 10, 20]


async def test_wait_for_telegram_stops_on_bad_token():
    bot = FlakyBot([TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")])
    with pytest.raises(TelegramUnauthorizedError):
        await app_module.wait_for_telegram(bot)  # type: ignore[arg-type]


def test_settings_parsing(monkeypatch, tmp_path):
    for key, value in {
        "BOT_TOKEN": "x",
        "ALLOWED_CHAT_IDS": "-1001, -1002",
        "BOT_ADMIN_IDS": "1;2",
        "REMINDER_OFFSETS_HOURS": "1, 24, 0.5",
        "GITHUB_TOKEN": "",
    }.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(tmp_path / "missing.env")
    assert settings.allowed_chat_ids == {-1001, -1002}
    assert settings.bot_admin_ids == frozenset({1, 2})
    assert settings.reminder_offsets_minutes == [1440, 60, 30]
    assert settings.github_token is None
    assert not settings.setup_mode

    monkeypatch.setenv("ALLOWED_CHAT_IDS", "")
    assert load_settings(tmp_path / "missing.env").setup_mode

    monkeypatch.setenv("BOT_ADMIN_IDS", "abc")
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "missing.env")


def test_tokens_are_redacted_from_logs():
    from bot.logging_setup import redact

    fake_tg = "1234567890:" + "A" * 35
    fake_gh = "github_pat_" + "B" * 40
    text = redact(f"url=https://api.telegram.org/bot{fake_tg}/getMe token={fake_gh}")
    assert fake_tg not in text and fake_gh not in text
