from __future__ import annotations

import os
from decimal import Decimal

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


def test_wavespeed_polling_defaults() -> None:
    settings = Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        _env_file=None,
    )

    assert settings.wavespeed_poll_fast_seconds == 10
    assert settings.wavespeed_poll_normal_seconds == 30
    assert settings.wavespeed_poll_slow_seconds == 60
    assert settings.wavespeed_poll_timeout_seconds == 1800


def test_generation_pricing_defaults() -> None:
    settings = Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        _env_file=None,
    )

    assert settings.pricing_markup_multiplier == Decimal("1.5")
    assert settings.usd_per_100_credits == Decimal("1.30")


def test_referral_bonus_defaults() -> None:
    settings = Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        _env_file=None,
    )

    assert settings.referral_referrer_bonus_credits == 5
    assert settings.referral_referred_bonus_credits == 0
