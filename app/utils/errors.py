"""Исключения и безопасные сообщения об ошибках."""
from __future__ import annotations

from typing import Optional


class AppUserFacingError(Exception):
    """Базовое исключение с безопасным сообщением для пользователя."""

    def __init__(self, user_message: str, *, log_message: Optional[str] = None):
        super().__init__(log_message or user_message)
        self.user_message = user_message
        self.log_message = log_message or user_message


class ImageUploadError(AppUserFacingError):
    """Ошибка загрузки пользовательского изображения."""


class WavespeedError(AppUserFacingError):
    """Базовая ошибка интеграции Wavespeed."""


class WavespeedNetworkError(WavespeedError):
    """Сетевая ошибка при обращении к Wavespeed."""


class WavespeedTimeoutError(WavespeedError):
    """Таймаут при обращении к Wavespeed."""


class WavespeedFailedError(WavespeedError):
    """Wavespeed вернул ошибку выполнения генерации."""


FORBIDDEN_ERROR_MARKERS = (
    "traceback",
    "authorization",
    "bearer ",
    "api key",
    "api_key",
    "token",
    "secret",
    "password",
    "file \"",
    "line ",
)


def sanitize_external_error_message(message: Optional[str]) -> Optional[str]:
    """Оставить только безопасное внешнее сообщение об ошибке."""
    if message is None:
        return None
    cleaned = " ".join(str(message).strip().split())
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if any(marker in lowered for marker in FORBIDDEN_ERROR_MARKERS):
        return None
    if len(cleaned) > 220:
        return None
    return cleaned


def get_friendly_error_message(exc: Exception) -> str:
    """Получить безопасное сообщение для пользователя."""
    if isinstance(exc, AppUserFacingError):
        return exc.user_message
    return "Что-то пошло не так. Попробуйте позже."