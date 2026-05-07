"""Locale helpers for bot translations."""
from __future__ import annotations

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = (
    "en",
    "ru",
    "es",
    "pt",
    "fr",
    "de",
    "ar",
    "hi",
    "zh",
    "id",
)

SUPPORTED_LANGUAGE_SET = frozenset(SUPPORTED_LANGUAGES)


def get_user_language(language_code: str | None) -> str:
    """Resolve a Telegram language code to a supported bot locale."""
    if language_code is None:
        return DEFAULT_LANGUAGE

    normalized = language_code.strip().lower()
    if not normalized:
        return DEFAULT_LANGUAGE

    base_language = normalized.split("-", maxsplit=1)[0].split("_", maxsplit=1)[0]
    if base_language in SUPPORTED_LANGUAGE_SET:
        return base_language
    return DEFAULT_LANGUAGE