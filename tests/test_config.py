from __future__ import annotations

import os
from decimal import Decimal

import pytest

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.config import Settings
from app.database_url import (
    database_connection_mode,
    database_url_host,
    is_private_database_endpoint,
    is_public_database_endpoint,
    normalize_database_url,
)


PRIVATE_RAILWAY_URL = "postgresql+asyncpg://postgres:password@postgres.railway.internal:5432/railway"
PUBLIC_RAILWAY_URL = "postgresql+asyncpg://postgres:password@shortline.proxy.rlwy.net:12345/railway"


def _clear_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in (
        "DATABASE_URL",
        "DATABASE_PRIVATE_URL",
        "DATABASE_PUBLIC_URL",
        "DATABASE_PUBLIC_FALLBACK_ENABLED",
        "STRICT_PRIVATE_NETWORK",
        "ENV",
    ):
        monkeypatch.delenv(env_name, raising=False)


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


def test_database_url_prefers_explicit_database_url_over_private_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)
    explicit_url = "postgresql+asyncpg://postgres:password@custom-db.example.com:5432/app"
    settings = Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        database_url=explicit_url,
        database_private_url=PRIVATE_RAILWAY_URL,
        _env_file=None,
    )

    assert settings.database_url == explicit_url


def test_database_url_prefers_private_url_when_database_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)
    settings = Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        database_private_url=PRIVATE_RAILWAY_URL,
        database_public_url=PUBLIC_RAILWAY_URL,
        database_public_fallback_enabled=True,
        _env_file=None,
    )

    assert settings.database_url == PRIVATE_RAILWAY_URL


def test_database_public_url_fallback_requires_explicit_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)
    disabled_settings = Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        database_public_url=PUBLIC_RAILWAY_URL,
        _env_file=None,
    )
    enabled_settings = Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        database_public_url=PUBLIC_RAILWAY_URL,
        database_public_fallback_enabled=True,
        _env_file=None,
    )

    assert disabled_settings.database_url == "sqlite+aiosqlite:///./bot.db"
    assert enabled_settings.database_url == PUBLIC_RAILWAY_URL


def test_strict_private_network_rejects_public_endpoint_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)
    with pytest.raises(Exception, match="STRICT_PRIVATE_NETWORK"):
        Settings(
            bot_token="test-bot-token",
            wavespeed_api_key="test-api-key",
            public_base_url="https://example.com",
            env="production",
            database_url=PUBLIC_RAILWAY_URL,
            strict_private_network=True,
            _env_file=None,
        )


def test_database_url_parser_detects_private_and_public_railway_domains() -> None:
    assert normalize_database_url("postgresql://postgres:password@postgres.railway.internal:5432/railway").startswith(
        "postgresql+asyncpg://"
    )
    assert database_url_host(PRIVATE_RAILWAY_URL) == "postgres.railway.internal"
    assert is_private_database_endpoint(PRIVATE_RAILWAY_URL) is True
    assert is_public_database_endpoint(PRIVATE_RAILWAY_URL) is False
    assert database_connection_mode(PRIVATE_RAILWAY_URL) == "private"

    assert database_url_host(PUBLIC_RAILWAY_URL) == "shortline.proxy.rlwy.net"
    assert is_public_database_endpoint(PUBLIC_RAILWAY_URL) is True
    assert is_private_database_endpoint(PUBLIC_RAILWAY_URL) is False
    assert database_connection_mode(PUBLIC_RAILWAY_URL) == "public"
