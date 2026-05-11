"""Modular generation model registry."""
from __future__ import annotations

from .alibaba import PROVIDER_MODELS as ALIBABA_MODELS
from .base import (
    GENERATION_CATEGORIES,
    GENERATION_TYPES,
    PROVIDERS,
    GenerationModel,
    GenerationSetting,
    SettingOption,
    apply_generated_model_params,
    build_model_registry,
    create_wavespeed_model_from_docs_url,
    get_default_allowed_payload_fields,
    get_default_required_payload_fields,
    infer_generation_type_from_endpoint,
    infer_generation_type_from_slug,
    infer_provider_from_url_or_slug,
    humanize_model_title,
    normalize_model_key,
    normalize_generation_type,
)
from .bytedance import PROVIDER_MODELS as BYTEDANCE_MODELS
from .generated_params import GENERATED_MODEL_PARAMS
from .google import PROVIDER_MODELS as GOOGLE_MODELS
from .grok import PROVIDER_MODELS as GROK_MODELS
from .kling import PROVIDER_MODELS as KLING_MODELS
from .minimax import PROVIDER_MODELS as MINIMAX_MODELS
from .openai import PROVIDER_MODELS as OPENAI_MODELS
from .wavespeed_ai import PROVIDER_MODELS as WAVESPEED_AI_MODELS

ALL_GENERATION_MODELS: tuple[GenerationModel, ...] = tuple(
    [
        *ALIBABA_MODELS,
        *BYTEDANCE_MODELS,
        *GOOGLE_MODELS,
        *OPENAI_MODELS,
        *KLING_MODELS,
        *GROK_MODELS,
        *MINIMAX_MODELS,
        *WAVESPEED_AI_MODELS,
    ]
)

MODEL_REGISTRY = build_model_registry(apply_generated_model_params(ALL_GENERATION_MODELS, GENERATED_MODEL_PARAMS))

LEGACY_MODEL_KEY_ALIASES = {
    "nano_banana": "google_nano_banana_pro_edit_ultra",
    "seedream": "bytedance_seedream_v4_5_edit",
    "gpt_image_2_text_to_image": "openai_gpt_image_2_text_to_image",
    "gpt_image_2_edit": "openai_gpt_image_2_edit",
    "seedream_v4_5": "bytedance_seedream_v4_5",
}

# Backward-compatible alias for existing imports.
GENERATION_MODELS = MODEL_REGISTRY


def get_generation_model(model_key: str) -> GenerationModel:
    """Получить конфигурацию модели по ключу."""
    try:
        canonical_key = LEGACY_MODEL_KEY_ALIASES.get(model_key, model_key)
        return MODEL_REGISTRY[canonical_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported generation model: {model_key}") from exc


def list_generation_models() -> list[GenerationModel]:
    """Получить список доступных моделей."""
    return [model for model in MODEL_REGISTRY.values() if model.is_enabled]


__all__ = [
    "ALL_GENERATION_MODELS",
    "MODEL_REGISTRY",
    "GENERATION_MODELS",
    "GENERATION_CATEGORIES",
    "GENERATION_TYPES",
    "PROVIDERS",
    "GenerationModel",
    "GenerationSetting",
    "SettingOption",
    "apply_generated_model_params",
    "build_model_registry",
    "create_wavespeed_model_from_docs_url",
    "get_default_allowed_payload_fields",
    "get_default_required_payload_fields",
    "get_generation_model",
    "infer_generation_type_from_endpoint",
    "infer_generation_type_from_slug",
    "infer_provider_from_url_or_slug",
    "humanize_model_title",
    "list_generation_models",
    "normalize_model_key",
    "normalize_generation_type",
]
