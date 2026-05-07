"""Bot i18n helpers."""
from __future__ import annotations

from app.i18n.locales import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, get_user_language
from app.i18n.translations import TRANSLATIONS


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Return a translated string with English fallback."""
    resolved_language = get_user_language(lang)
    language_catalog = TRANSLATIONS.get(resolved_language, {})
    fallback_catalog = TRANSLATIONS[DEFAULT_LANGUAGE]
    template = language_catalog.get(key, fallback_catalog.get(key, key))
    return template.format(**kwargs) if kwargs else template


__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "TRANSLATIONS",
    "get_user_language",
    "t",
]