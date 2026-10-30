from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from bot import app as app_module
from bot.config import ConfigError, load_settings
from tests.conftest import make_settings


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


async def test_missing_group_does_not_check_any_chat_rights():
    bot = SimpleNamespace(
        get_me=AsyncMock(
            return_value=SimpleNamespace(
                username="hacks_bot", id=4242, can_read_all_group_messages=False
            )
        ),
        get_chat_member=AsyncMock(),
        send_message=AsyncMock(),
    )
    await app_module.check_bot_rights(  # type: ignore[arg-type]
        bot, make_settings(allowed_chat_ids=set())
    )
    bot.get_chat_member.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_four_commands_replace_every_menu_scope():
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    await app_module.register_commands(  # type: ignore[arg-type]
        bot, make_settings(allowed_chat_ids={-1001})
    )
    calls = bot.set_my_commands.await_args_list
    assert [call.kwargs["scope"].type for call in calls] == [
        "default",
        "all_group_chats",
        "all_private_chats",
        "chat",
    ]
    assert [call.kwargs["scope"].chat_id for call in calls[3:]] == [-1001]
    assert all(call.args[0] == [] for call in calls[:3])
    for call in calls[3:]:
        assert [command.command for command in call.args[0]] == ["help", "hack", "me", "github"]
        assert all(command.is_ephemeral for command in call.args[0])


def test_settings_parsing(monkeypatch, tmp_path):
    for key, value in {
        "BOT_TOKEN": "x",
        "ALLOWED_CHAT_IDS": "-1001",
        "BOT_ADMIN_IDS": "1;2",
        "REMINDER_OFFSETS_HOURS": "1, 24, 0.5",
        "GITHUB_TOKEN": "",
    }.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(tmp_path / "missing.env")
    assert settings.allowed_chat_ids == {-1001}
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
    fake_llm = "sk-or-v1-" + "C" * 64
    text = redact(f"url=https://api.telegram.org/bot{fake_tg}/getMe {fake_gh} {fake_llm}")
    assert all(token not in text for token in (fake_tg, fake_gh, fake_llm))


def test_llm_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "openrouter/free")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1/")
    settings = load_settings(tmp_path / "missing.env")
    assert settings.llm_api_key == "test-secret"
    assert "test-secret" not in repr(settings)
    assert settings.llm_model == "openrouter/free"
    assert settings.llm_base_url == "https://openrouter.ai/api/v1"
    for url in ("http://example.com", "https://user:pass@example.com", "https://example.com?q=x"):
        monkeypatch.setenv("LLM_BASE_URL", url)
        with pytest.raises(ConfigError, match="LLM_BASE_URL"):
            load_settings(tmp_path / "missing.env")


def test_gigachat_settings_and_secret_repr(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setenv("LLM_PROVIDER", "gigachat")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "base64-secret")
    monkeypatch.setenv("GIGACHAT_SCOPE", "GIGACHAT_API_B2B")
    monkeypatch.setenv("GIGACHAT_MODEL", "GigaChat-Pro")
    settings = load_settings(tmp_path / "missing.env")
    assert settings.llm_provider == "gigachat"
    assert settings.gigachat_scope == "GIGACHAT_API_B2B"
    assert settings.gigachat_model == "GigaChat-Pro"
    assert settings.gigachat_credentials == "base64-secret"
    assert "base64-secret" not in repr(settings)
    monkeypatch.setenv("LLM_PROVIDER", "unknown")
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        load_settings(tmp_path / "missing.env")
    monkeypatch.setenv("LLM_PROVIDER", "gigachat")
    monkeypatch.setenv("GIGACHAT_SCOPE", "unknown")
    with pytest.raises(ConfigError, match="GIGACHAT_SCOPE"):
        load_settings(tmp_path / "missing.env")


@pytest.mark.parametrize("raw", ["123", "0", "-1001,-1002", "-1001,123"])
def test_group_configuration_rejects_private_ids_and_multiple_groups(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", raw)
    with pytest.raises(ConfigError, match="ALLOWED_CHAT_IDS"):
        load_settings(tmp_path / "missing.env")


async def test_persisted_migrations_keep_only_the_latest_group(storage):
    from tests.conftest import ALLOWED_CHAT

    middle, latest = ALLOWED_CHAT - 1, ALLOWED_CHAT - 2
    await storage.chats.migrate_chat(ALLOWED_CHAT, middle)
    await storage.chats.migrate_chat(middle, latest)
    settings = make_settings()
    await app_module.apply_known_migrations(settings, storage)
    assert settings.allowed_chat_ids == {latest}
    assert not settings.is_chat_allowed(ALLOWED_CHAT) and not settings.is_chat_allowed(middle)


async def test_persisted_migration_cycle_disables_access(storage):
    from tests.conftest import ALLOWED_CHAT

    other = ALLOWED_CHAT - 1
    await storage.chats.migrate_chat(ALLOWED_CHAT, other)
    await storage.chats.migrate_chat(other, ALLOWED_CHAT)
    settings = make_settings()
    await app_module.apply_known_migrations(settings, storage)
    assert settings.setup_mode


def test_settings_representation_hides_all_api_secrets():
    settings = make_settings(
        bot_token="telegram-secret",
        github_token="github-secret",
        llm_api_key="llm-secret",
        gigachat_credentials="giga-secret",
    )
    assert all(
        secret not in repr(settings)
        for secret in ("telegram-secret", "github-secret", "llm-secret", "giga-secret")
    )
