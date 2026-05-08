from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app import main


class FakeBot:
    async def get_me(self):
        return SimpleNamespace(id=123456789, username="imai_test_bot")


@pytest.mark.asyncio
async def test_log_bot_identity_includes_bot_and_instance(monkeypatch, caplog) -> None:
    monkeypatch.setattr(main.settings, "instance_name", "local-dev")
    caplog.set_level("INFO", logger="telegram_bot")
    bot = main.TelegramBot.__new__(main.TelegramBot)
    bot.bot = FakeBot()

    await bot.log_bot_identity()

    assert any(
        isinstance(record.msg, dict)
        and record.msg == {
            "action": "telegram_bot_identity",
            "bot_id": 123456789,
            "bot_username": "imai_test_bot",
            "instance_name": "local-dev",
        }
        for record in caplog.records
    )


def test_importing_main_does_not_start_polling() -> None:
    assert callable(main.main)
