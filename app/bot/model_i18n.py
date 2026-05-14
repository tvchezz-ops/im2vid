"""Localized presentation helpers for generation models and settings."""
from __future__ import annotations

import re
from typing import Any

from app.i18n import DEFAULT_LANGUAGE, get_user_language, t

_BRAND_WORDS = {
    "ai",
    "alibaba",
    "bytedance",
    "google",
    "gpt",
    "grok",
    "kling",
    "minimax",
    "nano",
    "openai",
    "seedance",
    "seedream",
    "wan",
}

_TITLE_SUFFIX_PATTERNS = (
    r"\btext\s+to\s+image\b",
    r"\bimage\s+to\s+image\b",
    r"\bimage\s+edit\b",
    r"\btext\s+to\s+video\b",
    r"\bimage\s+to\s+video\b",
    r"\breference\s+to\s+video\b",
    r"\bvideo\s+edit\b",
    r"\bvideo\s+extend\b",
    r"\bmotion\s+control\b",
    r"\baudio\s+to\s+video\b",
    r"\bvideo\s+to\s+audio\b",
    r"\beffects?\b",
)

_MODEL_KIND_BY_KEY_FRAGMENT = {
    "upscaler": "model.kind.image_upscaler",
    "lipsync": "generation.section_title.lipsync",
}


def _translation_or_empty(key: str, lang: str, **kwargs: Any) -> str:
    value = t(key, lang, **kwargs)
    return "" if value == key else value


def get_provider_display_label(provider: Any, lang: str = DEFAULT_LANGUAGE) -> str:
    """Return a localized provider label without exposing raw snake_case values."""
    provider_key = str(provider or "").strip().lower()
    translated = _translation_or_empty(f"provider.label.{provider_key}", lang)
    if translated:
        return translated
    return provider_key.replace("_", " ").strip().title() or _translation_or_empty("provider.label.unknown", lang) or "Provider"


def _clean_model_brand(raw_title: str, provider: str) -> str:
    title = raw_title.replace("Wavespeed AI", "Wan AI").replace("WaveSpeed AI", "Wan AI")
    provider_label = get_provider_display_label(provider, "en")
    for prefix in {provider_label, provider.replace("_", " ").title()}:
        if prefix and title.casefold().startswith(prefix.casefold() + " "):
            title = title[len(prefix):].strip()
    for pattern in _TITLE_SUFFIX_PATTERNS:
        title = re.sub(pattern, " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" -·")
    return title or raw_title.replace("Wavespeed AI", "Wan AI").strip()


def _model_kind_label(model: Any, lang: str) -> str:
    model_key = str(getattr(model, "key", "")).lower()
    for fragment, translation_key in _MODEL_KIND_BY_KEY_FRAGMENT.items():
        if fragment in model_key:
            label = _translation_or_empty(translation_key, lang)
            if label:
                return label
    generation_type = str(getattr(model, "generation_type", "") or "")
    return _translation_or_empty(f"generation.section_title.{generation_type}", lang)


def get_model_display_title(model: Any, lang: str = DEFAULT_LANGUAGE) -> str:
    """Return the model title as localized UI copy while preserving product names."""
    resolved_lang = get_user_language(lang)
    model_key = str(getattr(model, "key", "") or "")
    translated = _translation_or_empty(f"model.{model_key}.title", resolved_lang)
    if translated:
        return translated

    raw_title = str(getattr(model, "title", model_key) or model_key).replace("Wavespeed AI", "Wan AI")
    if resolved_lang == DEFAULT_LANGUAGE:
        return raw_title

    brand = _clean_model_brand(raw_title, str(getattr(model, "provider", "")))
    kind = _model_kind_label(model, resolved_lang)
    if kind and kind.casefold() not in brand.casefold():
        return f"{brand} · {kind}"
    return brand
