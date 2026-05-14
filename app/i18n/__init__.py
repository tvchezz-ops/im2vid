"""Bot i18n helpers."""
from __future__ import annotations

import logging

from app.i18n.locales import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, get_user_language
from app.i18n.translations import TRANSLATIONS


logger = logging.getLogger(__name__)


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Return a translated string with English fallback."""
    resolved_language = get_user_language(lang)
    language_catalog = TRANSLATIONS.get(resolved_language, {})
    fallback_catalog = TRANSLATIONS[DEFAULT_LANGUAGE]
    template = language_catalog.get(key, fallback_catalog.get(key, key))
    return template.format(**kwargs) if kwargs else template


def safe_t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Return localized user-facing text without ever exposing raw i18n keys."""
    resolved_language = get_user_language(lang)
    language_catalog = TRANSLATIONS.get(resolved_language, {})
    fallback_catalog = TRANSLATIONS[DEFAULT_LANGUAGE]
    template = language_catalog.get(key) or fallback_catalog.get(key)
    if template is None:
        logger.warning("Missing translation key: %s for language %s", key, resolved_language)
        template = language_catalog.get("errors.internal_retry") or fallback_catalog.get("errors.internal_retry") or "Something went wrong. Please try again."
    try:
        return template.format(**kwargs) if kwargs else template
    except Exception as exc:
        logger.warning("Translation formatting failed for key %s and language %s: %s", key, resolved_language, exc)
        fallback_template = language_catalog.get("errors.internal_retry") or fallback_catalog.get("errors.internal_retry") or "Something went wrong. Please try again."
        return fallback_template


__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "TRANSLATIONS",
    "get_user_language",
    "safe_t",
    "t",
]