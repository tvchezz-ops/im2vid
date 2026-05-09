"""Конфигурация приложения."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


def _parse_admin_ids(value: str | list[int] | None) -> list[int]:
    """Преобразовать строку из env в список ID администраторов."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raw_value = value.strip()
    if not raw_value:
        return []
    try:
        return [int(item.strip()) for item in raw_value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("ADMIN_IDS должны содержать только числа через запятую") from exc


class CommaSeparatedEnvSource(EnvSettingsSource):
    """Поддержка списка admin_ids в формате 1,2,3 вместо JSON."""

    def prepare_field_value(self, field_name, field, value, value_is_complex):
        if field_name == "admin_ids":
            return _parse_admin_ids(value)
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class CommaSeparatedDotEnvSource(DotEnvSettingsSource):
    """Поддержка списка admin_ids в формате 1,2,3 вместо JSON."""

    def prepare_field_value(self, field_name, field, value, value_is_complex):
        if field_name == "admin_ids":
            return _parse_admin_ids(value)
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из .env файла."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Bot
    bot_token: str = Field(..., description="Telegram Bot Token от @BotFather")
    instance_name: str = Field(default="", description="Optional deployment/instance name for startup logs")
    
    # API
    wavespeed_api_key: str = Field(..., description="API ключ для Wavespeed")
    public_base_url: str = Field(
        ...,
        description="Публичный базовый URL для доступа к временным media-файлам",
    )
    temp_media_dir: str = Field(
        default="tmp/media",
        description="Директория для временных входных media-файлов",
    )
    temp_media_ttl_minutes: int = Field(
        default=30,
        description="TTL временных media-файлов в минутах",
    )
    max_parallel_generations_per_user: int = Field(
        default=3,
        description="Максимальное количество активных generation_request на пользователя",
    )
    wavespeed_poll_fast_seconds: int = Field(
        default=10,
        description="Интервал polling Wavespeed в первые 3 минуты",
    )
    wavespeed_poll_normal_seconds: int = Field(
        default=30,
        description="Интервал polling Wavespeed с 3 до 10 минут",
    )
    wavespeed_poll_slow_seconds: int = Field(
        default=60,
        description="Интервал polling Wavespeed после 10 минут",
    )
    wavespeed_poll_timeout_seconds: int = Field(
        default=1800,
        description="Общий timeout polling Wavespeed в секундах",
    )
    store_input_media: bool = Field(
        default=False,
        description="Флаг совместимости: не сохранять входные media в БД",
    )
    store_output_urls: bool = Field(
        default=False,
        description="Флаг совместимости: не сохранять output URL в БД",
    )
    telegram_max_document_size_mb: int = Field(
        default=50,
        description="Максимальный размер документа для отправки через Telegram Bot API в МБ",
    )
    telegram_safe_document_size_mb: int = Field(
        default=45,
        description="Безопасный размер документа для отправки через Telegram Bot API в МБ",
    )
    telegram_stars_return_bot_username: str = Field(
        default="",
        description="Username текущего бота для возврата из wallet bot без @",
    )
    telegram_stars_webhook_secret: str = Field(
        default="",
        description="Секрет webhook callbacks от Telegram Stars wallet bot",
    )
    wallet_bot_username: Optional[str] = Field(
        default=None,
        alias="WALLET_BOT_USERNAME",
        description="Username отдельного wallet bot для Telegram Stars без @",
    )
    main_bot_username: str = Field(
        default="",
        description="Username основного Telegram бота без @ для возврата после внешних оплат",
    )
    nowpayments_api_key: str = Field(default="", description="NOWPayments API key")
    nowpayments_ipn_secret: str = Field(default="", description="NOWPayments IPN secret")
    nowpayments_base_url: str = Field(default="https://api.nowpayments.io", description="NOWPayments API base URL")
    nowpayments_success_url: str = Field(default="", description="Optional NOWPayments success URL")
    nowpayments_cancel_url: str = Field(default="", description="Optional NOWPayments cancel URL")
    credit_usd_price: Decimal = Field(default=Decimal("0.013"), description="USD price for one credit")
    pricing_markup_multiplier: Decimal = Field(
        default=Decimal("2"),
        description="Markup multiplier applied to provider generation prices",
    )
    usd_per_100_credits: Decimal = Field(
        default=Decimal("1.30"),
        description="USD price for 100 credits used for generation cost conversion",
    )
    r2_endpoint_url: str = Field(
        default="",
        description="Endpoint URL для Cloudflare R2",
    )
    r2_access_key_id: str = Field(
        default="",
        description="Access Key ID для Cloudflare R2",
    )
    r2_secret_access_key: str = Field(
        default="",
        description="Secret Access Key для Cloudflare R2",
    )
    r2_bucket_name: str = Field(
        default="",
        description="Имя bucket в Cloudflare R2",
    )
    r2_signed_url_ttl_seconds: int = Field(
        default=1800,
        description="TTL подписанного URL для Cloudflare R2 в секундах",
    )
    
    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///./bot.db",
        description="URL подключения к БД (SQLite для разработки, PostgreSQL для production)",
    )
    
    # Admin IDs
    admin_ids: list[int] = Field(
        default_factory=list,
        description="Список ID администраторов (через запятую в .env, например: 123456789,987654321)",
    )

    @field_validator("wallet_bot_username", mode="before")
    @classmethod
    def normalize_wallet_bot_username(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        username = str(value).strip().lstrip("@")
        return username or None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            CommaSeparatedEnvSource(settings_cls),
            CommaSeparatedDotEnvSource(settings_cls),
            file_secret_settings,
        )

    @property
    def main_bot_url(self) -> str:
        return f"https://t.me/{self.main_bot_username}"


def is_r2_configured() -> bool:
    """Проверить, что обязательная конфигурация Cloudflare R2 заполнена."""
    return all(
        (
            settings.r2_endpoint_url.strip(),
            settings.r2_access_key_id.strip(),
            settings.r2_secret_access_key.strip(),
            settings.r2_bucket_name.strip(),
        )
    )


def is_nowpayments_configured() -> bool:
    """Проверить, что обязательная конфигурация NOWPayments заполнена."""
    return all(
        (
            settings.nowpayments_api_key.strip(),
            settings.nowpayments_ipn_secret.strip(),
            settings.nowpayments_base_url.strip(),
        )
    )


# Создаем глобальный экземпляр настроек
try:
    settings = Settings()
except Exception as e:
    raise RuntimeError(
        f"❌ Ошибка при загрузке конфигурации:\n"
        f"{str(e)}\n\n"
        f"Убедитесь, что в .env файле заданы все обязательные переменные:\n"
        f"- BOT_TOKEN\n"
        f"- INSTANCE_NAME (опционально, имя deployment/instance для логов)\n"
        f"- WAVESPEED_API_KEY\n"
        f"- PUBLIC_BASE_URL\n"
        f"- TEMP_MEDIA_DIR (опционально, по умолчанию tmp/media)\n"
        f"- TEMP_MEDIA_TTL_MINUTES (опционально, по умолчанию 30)\n"
        f"- MAX_PARALLEL_GENERATIONS_PER_USER (опционально, по умолчанию 3)\n"
        f"- WAVESPEED_POLL_FAST_SECONDS (опционально, по умолчанию 10)\n"
        f"- WAVESPEED_POLL_NORMAL_SECONDS (опционально, по умолчанию 30)\n"
        f"- WAVESPEED_POLL_SLOW_SECONDS (опционально, по умолчанию 60)\n"
        f"- WAVESPEED_POLL_TIMEOUT_SECONDS (опционально, по умолчанию 1800)\n"
        f"- STORE_INPUT_MEDIA (опционально, по умолчанию false)\n"
        f"- STORE_OUTPUT_URLS (опционально, по умолчанию false)\n"
        f"- TELEGRAM_MAX_DOCUMENT_SIZE_MB (опционально, по умолчанию 50)\n"
        f"- TELEGRAM_SAFE_DOCUMENT_SIZE_MB (опционально, по умолчанию 45)\n"
        f"- TELEGRAM_STARS_RETURN_BOT_USERNAME (опционально, для возврата из wallet bot)\n"
        f"- TELEGRAM_STARS_WEBHOOK_SECRET (опционально, для webhook от wallet bot)\n"
        f"- WALLET_BOT_USERNAME (опционально, username отдельного wallet bot)\n"
        f"- MAIN_BOT_USERNAME (опционально, для возврата после внешних оплат)\n"
        f"- NOWPAYMENTS_API_KEY (опционально, для crypto payments)\n"
        f"- NOWPAYMENTS_IPN_SECRET (опционально, для crypto webhooks)\n"
        f"- NOWPAYMENTS_BASE_URL (опционально, по умолчанию https://api.nowpayments.io)\n"
        f"- NOWPAYMENTS_SUCCESS_URL (опционально, success redirect от NOWPayments)\n"
        f"- NOWPAYMENTS_CANCEL_URL (опционально, cancel redirect от NOWPayments)\n"
        f"- CREDIT_USD_PRICE (опционально, по умолчанию 0.013)\n"
        f"- PRICING_MARKUP_MULTIPLIER (опционально, по умолчанию 2)\n"
        f"- USD_PER_100_CREDITS (опционально, по умолчанию 1.30)\n"
        f"- R2_ENDPOINT_URL (опционально, для Cloudflare R2)\n"
        f"- R2_ACCESS_KEY_ID (опционально, для Cloudflare R2)\n"
        f"- R2_SECRET_ACCESS_KEY (опционально, для Cloudflare R2)\n"
        f"- R2_BUCKET_NAME (опционально, для Cloudflare R2)\n"
        f"- R2_SIGNED_URL_TTL_SECONDS (опционально, по умолчанию 1800)\n"
        f"- DATABASE_URL (опционально, есть значение по умолчанию)\n"
        f"- ADMIN_IDS (опционально, список чисел через запятую)"
    ) from e

