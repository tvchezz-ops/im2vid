"""Конфигурация приложения."""

from __future__ import annotations

from pydantic import Field
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
    
    # API
    wavespeed_api_key: str = Field(..., description="API ключ для Wavespeed")
    public_base_url: str = Field(
        ...,
        description="Публичный базовый URL для доступа к локально сохраненным media-файлам",
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


# Создаем глобальный экземпляр настроек
try:
    settings = Settings()
except Exception as e:
    raise RuntimeError(
        f"❌ Ошибка при загрузке конфигурации:\n"
        f"{str(e)}\n\n"
        f"Убедитесь, что в .env файле заданы все обязательные переменные:\n"
        f"- BOT_TOKEN\n"
        f"- WAVESPEED_API_KEY\n"
        f"- PUBLIC_BASE_URL\n"
        f"- DATABASE_URL (опционально, есть значение по умолчанию)\n"
        f"- ADMIN_IDS (опционально, список чисел через запятую)"
    ) from e

