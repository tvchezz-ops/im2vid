"""Generation models for the google provider."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .base import (
    COMMON_IMAGE_SYSTEM_SETTINGS,
    GenerationModel,
    NANO_BANANA_PRICING_RULES,
    NANO_BANANA_SETTINGS,
    VEO_3_1_PRICING_RULES,
    _select_setting,
    build_input_schema,
    create_wavespeed_model_from_docs_url,
)


GOOGLE_MODEL_SLUGS = (
    "google-nano-banana-pro-edit",
    "google-nano-banana-pro-edit-ultra",
    "google-nano-banana-pro-text-to-image",
    "google-nano-banana-edit",
    "google-nano-banana-text-to-image",
    "google-nano-banana-pro-text-to-image-ultra",
    "google-nano-banana-pro-edit-multi",
    "google-veo3.1-fast-image-to-video",
    "google-veo3.1-image-to-video",
    "google-gemini-2.5-flash-image-edit",
    "google-nano-banana-pro-text-to-image-multi",
    "google-veo3-image-to-video",
    "google-veo3.1-text-to-video",
    "google-imagen4",
    "google-gemini-2.5-flash-image-text-to-image",
    "google-veo3.1-reference-to-video",
    "google-veo3-fast-image-to-video",
    "google-imagen4-ultra",
    "google-veo3.1-fast-video-extend",
    "google-imagen3",
    "google-veo3.1-video-extend",
    "google-gemini-2.5-flash-image-preview-edit",
    "google-veo3-fast",
    "google-veo3",
    "google-gemini-2.5-flash-image-preview-text-to-image",
    "google-imagen4-fast",
    "google-imagen3-fast",
    "google-nano-banana-2-edit",
    "google-veo3.1-lite-image-to-video",
    "google-veo3.1-lite-start-end-to-video",
    "google-nano-banana-2-text-to-image",
    "google-nano-banana-2-text-to-image-fast",
    "google-veo2",
    "google-veo3.1-lite-text-to-video",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/google/{slug}"


def _with_schema(model: GenerationModel) -> GenerationModel:
    return replace(model, input_schema=build_input_schema(model))


def _apply_google_contract(model: GenerationModel) -> GenerationModel:
    if model.key == "google_nano_banana_pro_edit_ultra":
        return _with_schema(
            replace(
                model,
                max_images=14,
                supports_multiple_images=True,
                user_settings={**NANO_BANANA_SETTINGS, **model.user_settings},
                system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
                allowed_payload_fields=("images", "prompt", "aspect_ratio", "resolution", "output_format", "enable_sync_mode", "enable_base64_output"),
                base_wavespeed_price_usd=Decimal("0.14"),
                pricing_rules=NANO_BANANA_PRICING_RULES,
            )
        )
    if model.key in {"google_veo3", "google_veo3_fast"}:
        base_price = Decimal("0.12") if model.key == "google_veo3_fast" else Decimal("0.22")
        return _with_schema(
            replace(
                model,
                user_settings={
                    "duration": _select_setting("duration", "Длительность", "8", ("5", "8")),
                    "resolution": _select_setting("resolution", "Разрешение", "720p", ("720p", "1080p")),
                    "aspect_ratio": _select_setting("aspect_ratio", "Формат", "16:9", ("16:9", "9:16")),
                    **model.user_settings,
                },
                system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
                allowed_payload_fields=("prompt", "duration", "resolution", "aspect_ratio", "enable_sync_mode", "enable_base64_output"),
                base_wavespeed_price_usd=base_price,
                pricing_rules=VEO_3_1_PRICING_RULES,
            )
        )
    if model.key == "google_veo3_1_fast_video_extend":
        return _with_schema(
            replace(
                model,
                requires_prompt=True,
                required_payload_fields=("video", "prompt"),
                user_settings=model.user_settings,
                system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
                allowed_payload_fields=("video", "prompt", "enable_sync_mode", "enable_base64_output"),
                base_wavespeed_price_usd=Decimal("0.12"),
                pricing_rules=VEO_3_1_PRICING_RULES,
            )
        )
    return model


PROVIDER_MODELS: list[GenerationModel] = [
    _apply_google_contract(create_wavespeed_model_from_docs_url(_docs_url(slug), provider="google"))
    for slug in dict.fromkeys(GOOGLE_MODEL_SLUGS)
]
