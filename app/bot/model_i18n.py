"""Localized presentation helpers for generation models and settings."""
from __future__ import annotations

import re
from typing import Any, Literal

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
    r"\bspeech\s+to\s+video\b",
    r"\bvoice\s+to\s+video\b",
    r"\bvideo\s+to\s+audio\b",
    r"\beffects?\b",
)
_BUTTON_TITLE_SUFFIX_PATTERNS = (*_TITLE_SUFFIX_PATTERNS, r"\bedit\b")

ModelButtonTitleContext = Literal["category_list", "all_models_provider_list", "provider_models_list"]

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


def _clean_model_brand(raw_title: str, provider: str, suffix_patterns: tuple[str, ...] = _TITLE_SUFFIX_PATTERNS) -> str:
    title = raw_title.replace("Wavespeed AI", "Wan AI").replace("WaveSpeed AI", "Wan AI")
    provider_label = get_provider_display_label(provider, "en")
    for prefix in {provider_label, provider.replace("_", " ").title()}:
        if prefix and title.casefold().startswith(prefix.casefold() + " "):
            title = title[len(prefix):].strip()
    for pattern in suffix_patterns:
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
    if generation_type == "image_to_image":
        generation_type = "image_edit"
    if generation_type == "audio_to_video":
        generation_type = "lipsync"
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


def _get_model_short_title(model: Any) -> str:
    raw_title = str(getattr(model, "title", getattr(model, "key", "")) or getattr(model, "key", ""))
    return _clean_model_brand(raw_title, str(getattr(model, "provider", "")), _BUTTON_TITLE_SUFFIX_PATTERNS)


def format_model_button_title(
    model: Any,
    lang: str = DEFAULT_LANGUAGE,
    context: ModelButtonTitleContext = "category_list",
) -> str:
    """Return a model button title for the exact catalog surface."""
    if context == "provider_models_list":
        short_title = _get_model_short_title(model)
        kind = _model_kind_label(model, get_user_language(lang))
        if kind and kind.casefold() not in short_title.casefold():
            return f"{short_title} · {kind}"
        return short_title
    return _get_model_short_title(model)
