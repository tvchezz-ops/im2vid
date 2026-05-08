from __future__ import annotations

import os

import pytest

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.config import Settings


def _load_settings_with_wallet_env(monkeypatch: pytest.MonkeyPatch, raw_username: str | None) -> Settings:
    if raw_username is None:
        monkeypatch.delenv("WALLET_BOT_USERNAME", raising=False)
    else:
        monkeypatch.setenv("WALLET_BOT_USERNAME", raw_username)
    return Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        _env_file=None,
    )


def test_wallet_bot_username_reads_from_env_and_normalizes_at_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load_settings_with_wallet_env(monkeypatch, "@my_wallet_bot ")

    assert settings.wallet_bot_username == "my_wallet_bot"


def test_wallet_bot_username_empty_string_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load_settings_with_wallet_env(monkeypatch, "  ")

    assert settings.wallet_bot_username is None


def test_wallet_bot_username_missing_env_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load_settings_with_wallet_env(monkeypatch, None)

    assert settings.wallet_bot_username is None
