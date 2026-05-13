"""Smoke/unit tests for generation payload building."""

from __future__ import annotations

import os
import logging
import re
from decimal import Decimal

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.services.generation_service import (
    MODEL_REGISTRY,
    GENERATION_CATEGORIES,
    GENERATION_TYPES,
    PROVIDERS,
    GENERATION_MODELS,
    get_default_settings,
    build_model_registry,
    build_payload,
    calculate_generation_cost_credits,
    calculate_generation_price_quote,
    calculate_generation_price_usd,
    create_wavespeed_model_from_docs_url,
    get_generation_model,
    get_model_num_generations,
    is_contract_complete,
    GenerationModel,
    GenerationSetting,
    SettingOption,
    infer_generation_type_from_endpoint,
    infer_generation_type_from_slug,
    infer_provider_from_url_or_slug,
    humanize_model_title,
    list_generation_models,
    list_generation_types,
    list_models_by_provider,
    list_models_by_type,
    list_models_by_type_and_provider,
    list_providers,
    get_required_input_type,
    model_requires_image,
    model_requires_media,
    model_requires_video,
    normalize_model_key,
    is_generation_cost_estimated,
)
from app.services.model_registry.alibaba import ALIBABA_MODEL_SLUGS, PROVIDER_MODELS as ALIBABA_MODELS
from app.services.model_registry.bytedance import BYTEDANCE_MODEL_SLUGS, PROVIDER_MODELS as BYTEDANCE_MODELS
from app.services.model_registry.google import GOOGLE_MODEL_SLUGS, PROVIDER_MODELS as GOOGLE_MODELS
from app.services.model_registry.grok import GROK_MODEL_SLUGS, PROVIDER_MODELS as GROK_MODELS
from app.services.model_registry.kling import KLING_MODEL_SLUGS, PROVIDER_MODELS as KLING_MODELS
from app.services.model_registry.minimax import MINIMAX_MODEL_SLUGS, PROVIDER_MODELS as MINIMAX_MODELS
from app.services.model_registry.openai import OPENAI_MODEL_SLUGS, PROVIDER_MODELS as OPENAI_MODELS
from app.services.model_registry.wavespeed_ai import WAVESPEED_AI_MODEL_SLUGS, PROVIDER_MODELS as WAVESPEED_AI_MODELS
from app.services.model_registry.generated_params import GENERATED_MODEL_PARAMS
from app.services.model_registry.base import apply_generated_model_params
from scripts.audit_wavespeed_model_contracts import audit_model


def test_build_payload_nano_banana_defaults() -> None:
    payload = build_payload(
        "nano_banana",
        ["https://example.com/input.png"],
        "Make the image brighter and cleaner",
    )

    assert payload == {
        "images": ["https://example.com/input.png"],
        "prompt": "Make the image brighter and cleaner",
        "aspect_ratio": "1:1",
        "resolution": "4k",
        "output_format": "png",
    }


def test_build_payload_nano_banana_custom_settings() -> None:
    payload = build_payload(
        "nano_banana",
        ["https://example.com/input.png"],
        "Extend the scene to the left",
        {
            "aspect_ratio": "16:9",
            "resolution": "8k",
            "output_format": "jpeg",
        },
    )

    assert payload["aspect_ratio"] == "16:9"
    assert payload["resolution"] == "8k"
    assert payload["output_format"] == "jpeg"
    assert "enable_sync_mode" not in payload
    assert "enable_base64_output" not in payload


def test_build_payload_seedream_defaults() -> None:
    payload = build_payload(
        "seedream",
        ["https://example.com/input.png"],
        "Replace the background with a sunset cityscape",
    )

    assert payload == {
        "images": ["https://example.com/input.png"],
        "prompt": "Replace the background with a sunset cityscape",
    }


def test_build_payload_seedream_custom_settings() -> None:
    payload = build_payload(
        "seedream",
        ["https://example.com/input.png"],
        "Create a cinematic poster composition",
        {"size": "2048*2048"},
    )

    assert payload["size"] == "2048*2048"
    assert "enable_sync_mode" not in payload
    assert "enable_base64_output" not in payload


def test_build_payload_multi_image_models_keep_all_uploaded_images() -> None:
    payload = build_payload(
        "nano_banana",
        [
            "https://example.com/input-1.png",
            "https://example.com/input-2.png",
        ],
        "Blend both shots into one scene",
    )

    assert payload["images"] == [
        "https://example.com/input-1.png",
        "https://example.com/input-2.png",
    ]


def test_build_payload_invalid_model_key_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="Unsupported generation model: unknown_model"):
        build_payload(
            "unknown_model",
            ["https://example.com/input.png"],
            "Test prompt",
        )


def test_build_payload_invalid_setting_value_raises_validation_error() -> None:
    with pytest.raises(ValueError, match="Invalid value 'bad-format' for setting 'output_format'"):
        build_payload(
            "nano_banana",
            ["https://example.com/input.png"],
            "Test prompt for invalid setting",
            {"output_format": "bad-format"},
        )


def test_build_payload_non_string_setting_value_raises_validation_error() -> None:
    with pytest.raises(ValueError, match="must be a string value"):
        build_payload(
            "nano_banana",
            ["https://example.com/input.png"],
            "Test prompt for invalid type",
            {"output_format": 123},
        )


@pytest.mark.parametrize(
    ("model_key", "image_count", "expected_message"),
    [
        ("nano_banana", 15, "supports at most 10 images"),
        ("seedream", 11, "supports at most 10 images"),
    ],
)
def test_build_payload_rejects_too_many_images(
    model_key: str,
    image_count: int,
    expected_message: str,
) -> None:
    image_urls = [f"https://example.com/input-{index}.png" for index in range(image_count)]

    with pytest.raises(ValueError, match=expected_message):
        build_payload(model_key, image_urls, "Test prompt")


def test_build_payload_rejects_empty_prompt() -> None:
    with pytest.raises(ValueError, match="Prompt must not be empty"):
        build_payload(
            "nano_banana",
            ["https://example.com/input.png"],
            "   ",
        )


def test_build_payload_rejects_non_string_prompt() -> None:
    with pytest.raises(ValueError, match="Prompt must be a string"):
        build_payload(
            "nano_banana",
            ["https://example.com/input.png"],
            123,  # type: ignore[arg-type]
        )


def test_build_payload_rejects_non_string_image_urls() -> None:
    with pytest.raises(ValueError, match="All input URLs must be string values"):
        build_payload(
            "nano_banana",
            ["https://example.com/input.png", 123],  # type: ignore[list-item]
            "Valid prompt for invalid image types",
        )


def test_build_payload_ignores_blank_image_urls_and_requires_one_valid_image() -> None:
    with pytest.raises(ValueError, match="At least one image URL is required"):
        build_payload(
            "nano_banana",
            ["   ", ""],
            "Valid prompt",
        )


def test_build_payload_lipsync_requires_exact_media_and_audio_or_prompt() -> None:
    with pytest.raises(ValueError, match="Lipsync models require a video input"):
        build_payload("kwaivgi_kling_lipsync_audio_to_video", [], "", {"input_audio_url": "https://example.com/voice.mp3"})

    with pytest.raises(ValueError, match="Lipsync models require a video input"):
        build_payload("kwaivgi_kling_lipsync_audio_to_video", ["https://example.com/face.png"], "", {"input_audio_url": "https://example.com/voice.mp3"})

    with pytest.raises(ValueError, match="Lipsync audio-to-video models require audio input"):
        build_payload("kwaivgi_kling_lipsync_audio_to_video", ["https://example.com/face.mp4"], "")

    with pytest.raises(ValueError, match="missing_docs_contract"):
        build_payload("kwaivgi_kling_lipsync_text_to_video", ["https://example.com/face.mp4"], "   ")


def test_build_payload_lipsync_builds_video_audio_or_prompt_payload() -> None:
    audio_payload = build_payload(
        "kwaivgi_kling_lipsync_audio_to_video",
        [],
        "",
        {
            "input_video_url": "https://example.com/avatar.mp4",
            "input_audio_url": "https://example.com/voice.mp3",
        },
    )
    assert audio_payload == {
        "video": "https://example.com/avatar.mp4",
        "audio": "https://example.com/voice.mp3",
    }


def test_generation_model_exposes_new_fields_and_legacy_aliases() -> None:
    model = get_generation_model("nano_banana")

    assert model.key == "google_nano_banana_pro_edit_ultra"
    assert model.provider == "google"
    assert model.generation_type == "image_edit"
    assert model.type == "image_edit"
    assert model.model_type == "image_edit"
    assert model.docs_url.endswith("google/google-nano-banana-pro-edit-ultra")
    assert model.requires_prompt is True
    assert model.requires_image is True
    assert model.outputs == "image"
    assert model.is_enabled is True
    assert model.warning == ""
    assert model.required_payload_fields == ("images", "prompt")
    assert "images" in model.allowed_payload_fields
    assert "prompt" in model.allowed_payload_fields
    assert model.input_schema["required_payload_fields"] == ["images", "prompt"]
    assert model.required_fields == ("images", "prompt")
    assert model.input_media_field == "images"
    assert model.supports_multiple_images is True
    assert model.min_images == 1
    assert model.max_images == 10
    assert model.user_settings["num_generations"].default == "1"
    assert model.wavespeed_price_usd == Decimal("0.14")
    assert model.pricing_type == "per_generation"
    assert model.fallback_price_usd == Decimal("0.05")


def test_calculate_generation_cost_for_nano_banana_uses_markup_and_credit_price() -> None:
    model = get_generation_model("nano_banana")

    assert calculate_generation_price_usd(model, get_default_settings(model.key)) == Decimal("0.210")
    assert calculate_generation_cost_credits(model, get_default_settings(model.key)) == 17


def test_dynamic_price_log_includes_markup_multiplier(caplog) -> None:
    model = get_generation_model("nano_banana")

    with caplog.at_level(logging.INFO):
        calculate_generation_cost_credits(model, get_default_settings(model.key))

    assert any(
        isinstance(record.msg, dict)
        and record.msg.get("action") == "generation_dynamic_price_calculated"
        and record.msg.get("markup_multiplier") == "1.5"
        for record in caplog.records
    )


def test_calculate_generation_cost_recalculates_video_duration() -> None:
    model = get_generation_model("alibaba_wan_2_6_text_to_video")

    assert calculate_generation_cost_credits(model, {"duration": "5"}) == 6
    assert calculate_generation_cost_credits(model, {"duration": "10"}) == 6


def test_calculate_generation_cost_multiplies_num_generations() -> None:
    model = get_generation_model("nano_banana")

    assert calculate_generation_price_usd(model, get_default_settings(model.key), num_generations=3) == Decimal("0.630")
    assert calculate_generation_cost_credits(model, get_default_settings(model.key), num_generations=3) == 51
    assert calculate_generation_cost_credits(model, get_default_settings(model.key), num_generations=10) == 170
    assert calculate_generation_price_quote(model, get_default_settings(model.key), num_generations=10)[1] == 170


def test_wan_ai_image_upscaler_contract_payload_and_pricing() -> None:
    model = get_generation_model("wan_ai_image_upscaler")

    assert model.title == "Image Upscaler"
    assert model.provider == "wavespeed_ai"
    assert model.generation_type == "image_to_image"
    assert model.endpoint == "https://api.wavespeed.ai/api/v3/wavespeed-ai/image-upscaler"
    assert model.docs_url == "https://wavespeed.ai/docs/docs-api/wavespeed-ai/image-upscaler"
    assert model.requires_prompt is False
    assert model.requires_image is True
    assert model.requires_video is False
    assert model.requires_audio is False
    assert model.min_images == 1
    assert model.max_images == 1
    assert model.input_requirements["images"] == {"required": True, "min": 1, "max": 1, "payload_field": "image"}
    assert model.input_requirements["prompt"]["required"] is False
    assert model.input_requirements["video"]["required"] is False
    assert model.input_requirements["audio"]["required"] is False
    assert list(model.user_settings) == ["target_resolution", "output_format", "num_generations"]
    assert {key for key, setting in model.user_settings.items() if setting.is_user_visible} == {
        "target_resolution",
        "output_format",
        "num_generations",
    }
    assert "enable_base64_output" not in model.user_settings
    assert "enable_sync_mode" not in model.user_settings

    payload = build_payload(
        model.key,
        ["https://example.com/input.png"],
        "",
        {"target_resolution": "4k", "output_format": "jpeg", "num_generations": "10"},
    )

    assert payload == {
        "image": "https://example.com/input.png",
        "target_resolution": "4k",
        "output_format": "jpeg",
        "enable_base64_output": False,
        "enable_sync_mode": False,
    }
    assert model in list_models_by_type("image_to_image")
    assert model in list_models_by_provider("wavespeed_ai")
    assert calculate_generation_cost_credits(model, get_default_settings(model.key), num_generations=1) == 2
    assert calculate_generation_cost_credits(model, get_default_settings(model.key), num_generations=10) == 20


def test_calculate_generation_cost_ceil_credits() -> None:
    model = GenerationModel(
        key="ceil-model",
        title="Ceil Model",
        provider="google",
        generation_type="text_to_image",
        endpoint="https://example.com/text-to-image",
        docs_url="https://example.com/docs/text-to-image",
        description="Ceil price model",
        max_images=1,
        requires_prompt=True,
        requires_image=False,
        requires_video=False,
        requires_audio=False,
        outputs="image",
        base_wavespeed_price_usd=Decimal("0.0066"),
    )

    assert calculate_generation_price_usd(model, {}, num_generations=1) == Decimal("0.00990")
    assert calculate_generation_cost_credits(model, {}, num_generations=1) == 1


def test_resolution_multiplier_makes_1080p_more_expensive_than_720p() -> None:
    model = get_generation_model("google_veo3_fast")

    low = calculate_generation_cost_credits(model, {"duration": "5", "resolution": "720p", "aspect_ratio": "16:9"})
    high = calculate_generation_cost_credits(model, {"duration": "5", "resolution": "1080p", "aspect_ratio": "16:9"})

    assert high > low


def test_quality_multiplier_makes_high_more_expensive_than_fast() -> None:
    model = GenerationModel(
        key="quality-model",
        title="Quality Model",
        provider="google",
        generation_type="text_to_image",
        endpoint="https://example.com/text-to-image",
        docs_url="https://example.com/docs/text-to-image",
        description="Quality price model",
        max_images=1,
        requires_prompt=True,
        requires_image=False,
        requires_video=False,
        requires_audio=False,
        outputs="image",
        base_wavespeed_price_usd=Decimal("0.10"),
        pricing_rules={"quality_multipliers": {"fast": 1.0, "high": 2.2}},
    )

    assert calculate_generation_cost_credits(model, {"quality": "high"}) > calculate_generation_cost_credits(model, {"quality": "fast"})


def test_calculate_generation_cost_uses_fallback_price_when_unknown() -> None:
    model = GenerationModel(
        key="estimated-model",
        title="Estimated Model",
        provider="google",
        generation_type="text_to_image",
        endpoint="https://example.com/text-to-image",
        docs_url="https://example.com/docs/text-to-image",
        description="Estimated price model",
        max_images=1,
        requires_prompt=True,
        requires_image=False,
        requires_video=False,
        requires_audio=False,
        outputs="image",
    )

    assert is_generation_cost_estimated(model) is True
    assert calculate_generation_cost_credits(model, {}) == 6


def test_all_enabled_models_have_num_generations_setting() -> None:
    for model in list_generation_models():
        assert "num_generations" in model.user_settings
        assert model.user_settings["num_generations"].default == "1"
        assert [option.value for option in model.user_settings["num_generations"].options] == [str(value) for value in range(1, 11)]

    assert get_default_settings("nano_banana")["num_generations"] == "1"


def test_build_payload_does_not_include_internal_num_generations_setting() -> None:
    payload = build_payload(
        "bytedance_seedream_v4_sequential",
        [],
        "Generate a cinematic portrait",
        {"size": "2048*2048", "num_generations": "4"},
    )

    assert payload["prompt"] == "Generate a cinematic portrait"
    assert payload["size"] == "2048*2048"
    assert "num_generations" not in payload


def test_build_payload_accepts_internal_num_generations_up_to_ten() -> None:
    model = get_generation_model("google_veo3")

    assert get_model_num_generations(model, {"num_generations": "10"}) == 10
    assert get_model_num_generations(model, {"num_generations": "11"}) == 10

    payload = build_payload(
        "google_veo3",
        [],
        "Create a short atmospheric video",
        {"num_generations": "10", "duration": "8", "resolution": "720p", "aspect_ratio": "16:9"},
    )

    assert "num_generations" not in payload


def test_generation_registry_constants_include_supported_values() -> None:
    assert GENERATION_CATEGORIES == [
        "text_to_image",
        "image_to_image",
        "image_edit",
        "text_to_video",
        "image_to_video",
        "reference_to_video",
        "video_edit",
        "video_extend",
        "lipsync",
        "motion_control",
        "avatar",
        "audio_to_video",
        "video_to_audio",
        "effects",
        "all_models",
    ]
    assert GENERATION_TYPES == [
        "text_to_image",
        "image_to_image",
        "image_edit",
        "text_to_video",
        "image_to_video",
        "reference_to_video",
        "video_edit",
        "video_extend",
        "lipsync",
        "motion_control",
        "avatar",
        "audio_to_video",
        "video_to_audio",
        "effects",
    ]
    assert PROVIDERS == [
        "alibaba",
        "bytedance",
        "google",
        "openai",
        "kling",
        "grok",
        "minimax",
        "wavespeed_ai",
    ]

def test_required_input_type_helpers_follow_generation_type() -> None:
    assert get_required_input_type("text_to_image") == "text"
    assert get_required_input_type("text_to_video") == "text"
    assert get_required_input_type("image_to_image") == "image"
    assert get_required_input_type("image_edit") == "image"
    assert get_required_input_type("image_to_video") == "image"
    assert get_required_input_type("reference_to_video") == "image"
    assert get_required_input_type("video_edit") == "video"
    assert get_required_input_type("video_extend") == "video"
    assert get_required_input_type("video_to_video") == "video"
    assert get_required_input_type("lipsync") == "lipsync"
    assert get_required_input_type("avatar") == "lipsync"
    assert get_required_input_type("audio_to_video") == "lipsync"

    assert model_requires_media(get_generation_model("nano_banana")) is True
    assert model_requires_image(get_generation_model("nano_banana")) is True
    assert model_requires_video(get_generation_model("nano_banana")) is False
    assert model_requires_media(get_generation_model("google_veo3")) is False
    assert model_requires_video(get_generation_model("google_veo3_1_fast_video_extend")) is True


def test_model_registry_is_canonical_and_compatible() -> None:
    assert MODEL_REGISTRY is GENERATION_MODELS
    assert MODEL_REGISTRY["google_nano_banana_pro_edit_ultra"].provider == "google"
    assert MODEL_REGISTRY["bytedance_seedream_v4_5_edit"].provider == "bytedance"
    assert MODEL_REGISTRY["bytedance_seedream_v4_5_edit"].generation_type == "image_edit"
    assert "nano_banana" not in MODEL_REGISTRY
    assert "seedream" not in MODEL_REGISTRY


def test_all_model_keys_are_unique() -> None:
    keys = [model.key for model in MODEL_REGISTRY.values()]

    assert len(keys) == len(set(keys))
    assert set(keys) == set(MODEL_REGISTRY)


def test_every_enabled_model_has_required_registry_metadata_and_pricing_fields() -> None:
    for model in list_generation_models():
        docs_slug = model.docs_url.rstrip("/").rsplit("/", 1)[-1]

        assert model.key
        assert model.title
        assert model.provider in PROVIDERS
        assert model.generation_type in GENERATION_TYPES
        assert model.endpoint
        assert docs_slug
        endpoint_key = normalize_model_key(model.endpoint.rstrip("/").rsplit("/api/v3/", 1)[-1])
        aliased_endpoint_key = endpoint_key.replace("wavespeed_ai", "wan_ai", 1)
        assert endpoint_key.endswith(model.key) or model.key.endswith(endpoint_key) or aliased_endpoint_key.endswith(model.key)
        assert isinstance(model.base_wavespeed_price_usd, Decimal)
        assert model.base_wavespeed_price_usd > 0
        assert isinstance(model.wavespeed_price_usd, Decimal)
        assert model.wavespeed_price_usd > 0
        assert isinstance(model.fallback_price_usd, Decimal)
        assert model.fallback_price_usd > 0
        assert model.pricing_type in {"per_generation", "per_second_video"}
        assert calculate_generation_price_usd(model, get_default_settings(model.key)) > 0
        assert calculate_generation_cost_credits(model, get_default_settings(model.key)) > 0


def test_no_enabled_model_exposes_internal_seed_or_prompt_expansion_settings() -> None:
    for model in list_generation_models():
        assert "seed" not in model.user_settings
        assert "enable_prompt_expansion" not in model.user_settings


def test_build_model_registry_rejects_invalid_metadata() -> None:
    with pytest.raises(ValueError, match="Unsupported provider 'unknown'"):
        build_model_registry((
            GenerationModel(
                key="broken",
                title="Broken",
                provider="unknown",
                generation_type="image_edit",
                endpoint="https://example.com/broken",
                docs_url="https://example.com/docs/broken",
                description="Broken test model",
                max_images=1,
                requires_prompt=True,
                requires_image=False,
                requires_video=False,
                requires_audio=False,
                outputs="image",
            ),
        ))


@pytest.mark.parametrize(
    ("endpoint", "expected_generation_type"),
    [
        ("https://wavespeed.ai/docs-api/google/text-to-image/model", "text_to_image"),
        ("https://wavespeed.ai/docs-api/google/text-to-video/model", "text_to_video"),
        ("https://wavespeed.ai/docs-api/google/image-to-image/model", "image_to_image"),
        ("https://wavespeed.ai/docs-api/google/image-edit/model", "image_edit"),
        ("https://wavespeed.ai/docs-api/google/image-to-video/model", "image_to_video"),
        ("https://wavespeed.ai/docs-api/google/reference-to-video/model", "reference_to_video"),
        ("https://wavespeed.ai/docs-api/google/video-extend/model", "video_extend"),
        ("https://wavespeed.ai/docs-api/google/video-to-video/model", "video_edit"),
        ("https://wavespeed.ai/docs-api/google/video-edit/model", "video_edit"),
        ("https://wavespeed.ai/docs-api/google/lipsync/model", "lipsync"),
        ("https://wavespeed.ai/docs-api/google/talking-avatar/model", "avatar"),
        ("https://wavespeed.ai/docs-api/google/speech-to-video/model", "audio_to_video"),
        ("https://wavespeed.ai/docs-api/google/voice-to-video/model", "audio_to_video"),
        ("https://wavespeed.ai/docs-api/google/audio-to-video/model", "audio_to_video"),
        ("https://wavespeed.ai/docs-api/google/video-to-audio/model", "video_to_audio"),
        ("https://wavespeed.ai/docs-api/google/motion-control/model", "motion_control"),
        ("https://wavespeed.ai/docs-api/google/effects/model", "effects"),
        ("https://wavespeed.ai/docs-api/google/video-to-video-talking-avatar/model", "avatar"),
        ("https://wavespeed.ai/docs-api/google/unknown/model", ""),
    ],
)
def test_infer_generation_type_from_endpoint(endpoint: str, expected_generation_type: str) -> None:
    assert infer_generation_type_from_endpoint(endpoint) == expected_generation_type


@pytest.mark.parametrize(
    ("slug", "expected_generation_type"),
    [
        ("google-text-to-image", "text_to_image"),
        ("google-t2i-fast", "text_to_image"),
        ("seedream-image-to-image", "image_to_image"),
        ("openai-image-edit", "image_edit"),
        ("gpt-image-2-edit", "image_edit"),
        ("google-text-to-video", "text_to_video"),
        ("google-t2v-fast", "text_to_video"),
        ("alibaba-image-to-video", "image_to_video"),
        ("alibaba-i2v-pro", "image_to_video"),
        ("kling-reference-to-video", "reference_to_video"),
        ("runway-video-edit", "video_edit"),
        ("google-video-extend", "video_extend"),
        ("bytedance-motion-control", "motion_control"),
        ("bytedance-lipsync", "lipsync"),
        ("kwaivgi-kling-lipsync-text-to-video", "lipsync"),
        ("wavespeed-audio-to-video", "audio_to_video"),
        ("wan-2.2-speech-to-video", "audio_to_video"),
        ("wavespeed-video-to-audio", "video_to_audio"),
        ("bytedance-avatar-omni-human", "avatar"),
        ("wavespeed-effects-pack", "effects"),
        ("x-ai-grok-2-image", "text_to_image"),
        ("minimax-hailuo-02-fast", "text_to_video"),
        ("kwaivgi-kling-elements", "reference_to_video"),
        ("unknown-model", ""),
    ],
)
def test_infer_generation_type_from_slug(slug: str, expected_generation_type: str) -> None:
    assert infer_generation_type_from_slug(slug) == expected_generation_type


@pytest.mark.parametrize(
    ("value", "expected_provider"),
    [
        ("https://wavespeed.ai/docs/docs-api/alibaba/alibaba-wan-2.6-text-to-video", "alibaba"),
        ("bytedance-seedream-v4.5-edit", "bytedance"),
        ("google-veo3-fast", "google"),
        ("openai-gpt-image-2-edit", "openai"),
        ("kling-video-extend", "kling"),
        ("x-ai-grok-2-image", "grok"),
        ("grok-text-to-image", "grok"),
        ("minimax-text-to-video", "minimax"),
        ("wavespeed-ai-effects", "wavespeed_ai"),
        ("unknown-model", ""),
    ],
)
def test_infer_provider_from_url_or_slug(value: str, expected_provider: str) -> None:
    assert infer_provider_from_url_or_slug(value) == expected_provider


def test_normalize_model_key_from_slug() -> None:
    assert normalize_model_key("Google/Veo3 Fast: Text-to-Video") == "google_veo3_fast_text_to_video"


def test_humanize_model_title_from_slug() -> None:
    assert humanize_model_title("openai-gpt-image-2-edit") == "OpenAI GPT Image 2 Edit"
    assert humanize_model_title("wavespeed-ai-t2v") == "Wan AI T2V"


def test_create_wavespeed_model_from_docs_url_creates_valid_model() -> None:
    model = create_wavespeed_model_from_docs_url(
        "https://wavespeed.ai/docs/docs-api/google/google-nano-banana-pro-edit-ultra"
    )
    registry = build_model_registry((model,))

    registered_model = registry[model.key]
    assert model.key == "google_nano_banana_pro_edit_ultra"
    assert model.title == "Google Nano Banana Pro Edit Ultra"
    assert model.provider == "google"
    assert model.generation_type == "image_edit"
    assert model.base_wavespeed_price_usd == Decimal("0.05")
    assert model.pricing_type == "per_generation"
    assert set(registered_model.user_settings) >= {"num_generations", "strength", "negative_prompt"}
    assert "seed" not in registered_model.user_settings
    assert "enable_prompt_expansion" not in registered_model.user_settings


def test_create_wavespeed_model_from_docs_url_uses_unique_key_format() -> None:
    model = create_wavespeed_model_from_docs_url(
        "https://wavespeed.ai/docs/docs-api/alibaba/alibaba-wan-2.6-text-to-video"
    )

    assert model.key == "alibaba_wan_2_6_text_to_video"
    assert re.fullmatch(r"[a-z0-9_]+", model.key)


def test_create_wavespeed_model_from_docs_url_title_is_human_readable() -> None:
    model = create_wavespeed_model_from_docs_url(
        "https://wavespeed.ai/docs/docs-api/openai/openai-gpt-image-2-edit"
    )

    assert model.title == "OpenAI GPT Image 2 Edit"


def test_create_wavespeed_model_from_docs_url_image_input_rules() -> None:
    model = create_wavespeed_model_from_docs_url(
        "https://wavespeed.ai/docs/docs-api/openai/openai-gpt-image-2-edit"
    )

    assert model.outputs == "image"
    assert model.requires_prompt is True
    assert model.requires_image is True
    assert model.requires_video is False
    assert model.requires_audio is False
    assert model.input_media_field == "images"
    assert model.required_payload_fields == ("images", "prompt")
    assert set(model.user_settings) == {"num_generations"}


def test_create_wavespeed_model_from_docs_url_video_input_rules() -> None:
    text_to_video = create_wavespeed_model_from_docs_url(
        "https://wavespeed.ai/docs/docs-api/google/google-veo3-text-to-video"
    )
    image_to_video = create_wavespeed_model_from_docs_url(
        "https://wavespeed.ai/docs/docs-api/alibaba/alibaba-wan-2.6-image-to-video"
    )
    video_edit = create_wavespeed_model_from_docs_url(
        "https://wavespeed.ai/docs/docs-api/google/google-veo3-video-edit"
    )

    assert text_to_video.outputs == "video"
    assert text_to_video.requires_prompt is True
    assert text_to_video.requires_image is False
    assert text_to_video.requires_video is False
    assert text_to_video.required_payload_fields == ("prompt",)
    assert text_to_video.pricing_type == "per_generation"
    assert image_to_video.outputs == "video"
    assert image_to_video.requires_image is True
    assert image_to_video.input_media_field == "image"
    assert image_to_video.required_payload_fields == ("image", "prompt")
    assert video_edit.outputs == "video"
    assert video_edit.requires_video is True
    assert video_edit.input_media_field == "video"
    assert video_edit.required_payload_fields == ("video", "prompt")


def test_alibaba_provider_model_keys_are_unique() -> None:
    keys = [model.key for model in ALIBABA_MODELS]

    assert len(keys) == len(set(keys))
    assert len(keys) == len(dict.fromkeys(ALIBABA_MODEL_SLUGS))


def test_alibaba_provider_models_use_alibaba_provider() -> None:
    assert {model.provider for model in ALIBABA_MODELS} == {"alibaba"}


def test_alibaba_provider_model_categories_are_inferred_correctly() -> None:
    expected_categories = {
        "alibaba_wan_2_7_image_to_video": "image_to_video",
        "alibaba_wan_2_7_image_edit": "image_edit",
        "alibaba_wan_2_7_video_edit": "video_edit",
        "alibaba_wan_2_7_image_edit_pro": "image_edit",
        "alibaba_wan_2_7_text_to_video": "text_to_video",
        "alibaba_wan_2_7_reference_to_video": "reference_to_video",
        "alibaba_wan_2_7_video_extend": "video_extend",
        "alibaba_wan_2_7_text_to_image_pro": "text_to_image",
        "alibaba_wan_2_7_text_to_image": "text_to_image",
        "alibaba_happyhorse_1_0_text_to_video": "text_to_video",
        "alibaba_happyhorse_1_0_video_extend": "video_extend",
        "alibaba_happyhorse_1_0_video_edit": "video_edit",
        "alibaba_happyhorse_1_0_reference_to_video": "reference_to_video",
        "alibaba_happyhorse_1_0_image_to_video": "image_to_video",
        "alibaba_wan_2_6_image_to_video": "image_to_video",
        "alibaba_wan_2_6_image_edit": "image_edit",
        "alibaba_wan_2_6_image_to_video_flash": "image_to_video",
        "alibaba_wan_2_6_text_to_image": "text_to_image",
        "alibaba_wan_2_6_text_to_video": "text_to_video",
        "alibaba_wan_2_6_reference_to_video_flash": "reference_to_video",
        "alibaba_wan_2_6_reference_to_video": "reference_to_video",
        "alibaba_wan_2_6_image_to_video_spicy": "image_to_video",
        "alibaba_wan_2_6_video_extend": "video_extend",
        "alibaba_wan_2_6_image_to_video_pro": "image_to_video",
        "alibaba_wan_2_2_i2v_plus_1080p": "image_to_video",
        "alibaba_wan_2_2_i2v_plus_480p": "image_to_video",
        "alibaba_wan_2_2_t2v_plus_1080p": "text_to_video",
        "alibaba_wan_2_2_t2v_plus_480p": "text_to_video",
        "alibaba_wan_2_5_image_to_video": "image_to_video",
        "alibaba_wan_2_5_video_extend": "video_extend",
        "alibaba_wan_2_5_image_edit": "image_edit",
        "alibaba_wan_2_5_text_to_video": "text_to_video",
        "alibaba_wan_2_5_text_to_image": "text_to_image",
    }

    assert {model.key: model.generation_type for model in ALIBABA_MODELS} == expected_categories


def test_bytedance_provider_model_keys_are_unique() -> None:
    keys = [model.key for model in BYTEDANCE_MODELS]

    assert len(keys) == len(set(keys))
    assert len(keys) == len(dict.fromkeys(BYTEDANCE_MODEL_SLUGS))


def test_bytedance_provider_models_use_bytedance_provider() -> None:
    assert {model.provider for model in BYTEDANCE_MODELS} == {"bytedance"}


def test_google_provider_model_keys_are_unique() -> None:
    keys = [model.key for model in GOOGLE_MODELS]

    assert len(keys) == len(set(keys))
    assert len(keys) == len(dict.fromkeys(GOOGLE_MODEL_SLUGS))


def test_openai_provider_model_keys_are_unique() -> None:
    keys = [model.key for model in OPENAI_MODELS]

    assert len(keys) == len(set(keys))
    assert len(keys) == len(dict.fromkeys(OPENAI_MODEL_SLUGS))


@pytest.mark.parametrize(
    ("models", "slugs"),
    [
        (KLING_MODELS, KLING_MODEL_SLUGS),
        (GROK_MODELS, GROK_MODEL_SLUGS),
        (MINIMAX_MODELS, MINIMAX_MODEL_SLUGS),
        (WAVESPEED_AI_MODELS, WAVESPEED_AI_MODEL_SLUGS),
    ],
)
def test_remaining_provider_model_keys_are_unique(
    models: list[GenerationModel],
    slugs: tuple[str, ...],
) -> None:
    keys = [model.key for model in models]
    expected_count = len(dict.fromkeys(slugs))
    if models is WAVESPEED_AI_MODELS:
        expected_count += 1

    assert len(keys) == len(set(keys))
    assert len(keys) == expected_count


@pytest.mark.parametrize(
    ("models", "provider"),
    [
        (KLING_MODELS, "kling"),
        (GROK_MODELS, "grok"),
        (MINIMAX_MODELS, "minimax"),
        (WAVESPEED_AI_MODELS, "wavespeed_ai"),
    ],
)
def test_remaining_provider_models_use_internal_provider_key(
    models: list[GenerationModel],
    provider: str,
) -> None:
    assert {model.provider for model in models} == {provider}


def test_kling_specialty_models_are_in_expected_categories() -> None:
    assert {model.key for model in list_models_by_type("lipsync")} >= {
        "kwaivgi_kling_lipsync_audio_to_video",
    }
    assert get_generation_model("kwaivgi_kling_lipsync_text_to_video").hidden_reason == "missing_docs_contract"
    assert "kwaivgi_kling_effects" not in {model.key for model in list_generation_models()}
    assert get_generation_model("kwaivgi_kling_effects").is_enabled is False
    assert "kwaivgi_kling_video_to_audio" in {model.key for model in list_models_by_type("video_to_audio")}


def test_wavespeed_ai_wan_2_2_speech_to_video_accepts_audio_input() -> None:
    payload = build_payload(
        "wan_2_2_speech_to_video",
        ["https://example.com/avatar.png"],
        "",
        {"input_audio_url": "https://example.com/speech.mp3"},
    )

    assert payload == {
        "audio": "https://example.com/speech.mp3",
        "image": "https://example.com/avatar.png",
        "resolution": "480p",
    }


def test_enabled_models_reject_missing_required_media_before_confirm_payload() -> None:
    for model in list_generation_models():
        if not model.input_media_field:
            continue

        prompt = "Smoke test prompt" if model.requires_prompt else ""
        user_settings = {"input_audio_url": "https://example.com/input.mp3"} if model.requires_audio else {}

        with pytest.raises(ValueError):
            build_payload(model.key, [], prompt, user_settings)


def test_text_models_do_not_require_media() -> None:
    for model in list_generation_models():
        if model.generation_type not in {"text_to_image", "text_to_video"}:
            continue

        payload = build_payload(model.key, [], "Generate a clean cinematic result")

        assert payload["prompt"] == "Generate a clean cinematic result"
        assert "image" not in payload
        assert "images" not in payload
        assert "video" not in payload


def test_video_extend_requires_video_and_prompt() -> None:
    model = get_generation_model("google_veo3_1_fast_video_extend")

    assert model.input_media_field == "video"
    assert model.required_payload_fields == ("video",)
    with pytest.raises(ValueError, match="At least one video URL is required"):
        build_payload(model.key, [], "Extend this scene")
    assert build_payload(model.key, ["https://example.com/input.mp4"], "Extend this scene") == {
        "video": "https://example.com/input.mp4",
        "prompt": "Extend this scene",
        "resolution": "1080p",
    }


def test_openai_sora_models_are_classified_as_video() -> None:
    sora_models = [model for model in OPENAI_MODELS if "sora" in model.key]

    assert sora_models
    assert {model.outputs for model in sora_models} == {"video"}
    assert {model.generation_type for model in sora_models} <= {"text_to_video", "image_to_video"}
    assert get_generation_model("openai_sora_2_image_to_video").generation_type == "image_to_video"
    assert get_generation_model("openai_sora_2_text_to_video").generation_type == "text_to_video"


def test_openai_gpt_image_models_are_classified_as_image_text_or_edit() -> None:
    gpt_image_models = [model for model in OPENAI_MODELS if "gpt_image" in model.key]

    assert gpt_image_models
    assert {model.outputs for model in gpt_image_models} == {"image"}
    assert {model.generation_type for model in gpt_image_models} <= {"text_to_image", "image_edit"}
    assert get_generation_model("openai_gpt_image_2_edit").generation_type == "image_edit"
    assert get_generation_model("openai_gpt_image_2_text_to_image").generation_type == "text_to_image"


def test_build_payload_text_to_video_does_not_require_media() -> None:
    payload = build_payload(
        "google_veo3",
        [],
        "Create a smooth cinematic flythrough of a modern apartment",
    )

    assert payload["prompt"] == "Create a smooth cinematic flythrough of a modern apartment"
    assert "video" not in payload
    assert "image" not in payload


def test_build_model_registry_infers_generation_type_from_endpoint() -> None:
    registry = build_model_registry((
        GenerationModel(
            key="inferred-model",
            title="Inferred Model",
            provider="google",
            generation_type="",
            endpoint="https://wavespeed.ai/docs-api/google/text-to-image/inferred-model",
            docs_url="https://example.com/docs/inferred-model",
            description="Inferred test model",
            max_images=1,
            requires_prompt=True,
            requires_image=False,
            requires_video=False,
            requires_audio=False,
            outputs="image",
        ),
    ))

    assert registry["inferred-model"].generation_type == "text_to_image"


def test_build_model_registry_ignores_model_when_generation_type_cannot_be_inferred() -> None:
    registry = build_model_registry((
        GenerationModel(
            key="ignored-model",
            title="Ignored Model",
            provider="google",
            generation_type="",
            endpoint="https://wavespeed.ai/docs-api/google/custom-endpoint/ignored-model",
            docs_url="https://example.com/docs/ignored-model",
            description="Ignored test model",
            max_images=1,
            requires_prompt=True,
            requires_image=False,
            requires_video=False,
            requires_audio=False,
            outputs="image",
        ),
    ))

    assert registry == {}


def test_list_generation_models_returns_all_registry_models() -> None:
    assert list_generation_models() == [model for model in MODEL_REGISTRY.values() if model.is_enabled]


def test_enabled_models_have_settings_or_explicit_empty_state() -> None:
    for model in list_generation_models():
        defaults = get_default_settings(model.key)
        assert model.user_settings or defaults == {}
        assert model.required_payload_fields
        assert model.input_schema["required_payload_fields"] == list(model.required_payload_fields)


def test_model_specific_defaults_are_used_for_docs_confirmed_models() -> None:
    model = get_generation_model("alibaba_wan_2_6_text_to_image")

    assert {"size", "num_generations"} <= set(model.user_settings)
    assert get_default_settings(model.key) == {"size": "1024*1024", "num_generations": "1"}


def test_generated_params_import_and_reference_registry_models() -> None:
    assert GENERATED_MODEL_PARAMS
    assert set(GENERATED_MODEL_PARAMS).issubset(MODEL_REGISTRY)


def test_generated_params_are_merged_into_registry() -> None:
    model = get_generation_model("google_nano_banana_pro_edit_ultra")

    assert model.max_images == 10
    assert "aspect_ratio" in model.user_settings
    assert "aspect_ratio" in model.allowed_payload_fields
    assert "enable_sync_mode" not in model.system_settings


def test_target_models_have_generated_params_beyond_num_generations() -> None:
    for model_key in ("alibaba_wan_2_6_image_to_video_flash",):
        model = get_generation_model(model_key)
        generated = GENERATED_MODEL_PARAMS[model.key]

        assert generated["allowed_payload_fields"]
        assert len(model.user_settings) > 1
        assert "num_generations" in model.user_settings


def test_wan_flash_generated_settings_are_visible_and_validated() -> None:
    model = get_generation_model("alibaba_wan_2_6_image_to_video_flash")

    assert {"duration", "resolution", "shot_type", "negative_prompt", "enable_audio"} <= set(model.user_settings)
    assert "enable_prompt_expansion" not in model.user_settings
    assert "seed" not in model.user_settings

    payload = build_payload(
        model.key,
        ["https://example.com/input.png"],
        "Animate this scene",
        {"duration": "15", "resolution": "1080p", "shot_type": "multi", "enable_audio": "false", "negative_prompt": "blur"},
    )

    assert payload["image"] == "https://example.com/input.png"
    assert payload["duration"] == "15"
    assert payload["resolution"] == "1080p"
    assert payload["shot_type"] == "multi"
    assert payload["enable_audio"] is False
    assert payload["negative_prompt"] == "blur"


def test_lipsync_audio_model_has_generated_audio_setting_and_flow_fields() -> None:
    model = get_generation_model("kwaivgi_kling_lipsync_audio_to_video")

    assert "audio" not in model.user_settings
    assert model.input_media_field == "video"
    assert model.requires_audio is True
    assert set(model.required_payload_fields) == {"audio", "video"}
    assert {"audio", "video"} <= set(model.allowed_payload_fields)
    assert model.input_requirements["prompt"] == {"required": False, "payload_field": "prompt"}
    assert model.input_requirements["video"] == {"required": True, "payload_field": "video"}
    assert model.input_requirements["audio"] == {
        "required": True,
        "payload_field": "audio",
        "max_size_mb": 5,
        "file_types": [".aac", ".m4a", ".mp3", ".wav"],
    }


def test_audit_passes_for_all_enabled_models() -> None:
    problems = {model.key: audit_model(model) for model in list_generation_models()}
    problems = {model_key: issues for model_key, issues in problems.items() if issues}

    assert problems == {}


def test_all_enabled_models_have_generated_contract_mapping() -> None:
    for model in list_generation_models():
        assert is_contract_complete(model), model.key
        assert model.payload_mapping, model.key
        assert set(model.required_payload_fields) <= set(model.allowed_payload_fields), model.key


def test_generated_params_runtime_fallback_keeps_only_num_generations_under_20_percent() -> None:
    enabled_models = list_generation_models()
    only_num_generations = [
        model.key
        for model in enabled_models
        if {key for key, setting in model.user_settings.items() if setting.is_user_visible} <= {"num_generations"}
    ]

    assert len(only_num_generations) / len(enabled_models) <= 0.2, only_num_generations[:30]


def test_core_video_and_image_models_get_relevant_fallback_settings() -> None:
    setting_expectations = {
        "text_to_image": {"aspect_ratio", "negative_prompt", "resolution", "size"},
        "image_to_image": {"strength", "negative_prompt", "size", "target_resolution", "output_format"},
        "image_edit": {"strength", "negative_prompt", "size", "aspect_ratio", "resolution", "output_format", "guidance_scale"},
        "text_to_video": {"duration", "aspect_ratio", "mode", "quality", "negative_prompt", "size"},
        "image_to_video": {"duration", "mode", "quality", "motion_strength", "aspect_ratio"},
        "reference_to_video": {"duration", "mode", "quality", "motion_strength", "aspect_ratio", "resolution", "negative_prompt"},
    }

    for model in list_generation_models():
        expected_settings = setting_expectations.get(model.generation_type)
        if expected_settings is None:
            continue
        if set(model.user_settings) <= {"num_generations"}:
            continue
        assert expected_settings & set(model.user_settings), model.key


def test_deterministic_input_requirements_follow_generation_type() -> None:
    for model in list_generation_models():
        requirements = model.input_requirements
        if model.generation_type == "image_to_video":
            assert requirements["images"]["required"] is True, model.key
            assert requirements["prompt"]["required"] is True, model.key
        if model.generation_type == "text_to_video":
            assert requirements["prompt"]["required"] is True, model.key
            assert "images" not in requirements or requirements["images"]["required"] is False, model.key
            assert "video" not in requirements or requirements["video"]["required"] is False, model.key
        if model.generation_type == "video_edit":
            assert requirements["video"]["required"] is True, model.key
            assert requirements["prompt"]["required"] is True, model.key


def test_specific_generated_and_fallback_model_contracts() -> None:
    wan = get_generation_model("alibaba_wan_2_6_image_to_video_flash")
    kling = get_generation_model("kwaivgi_kling_lipsync_audio_to_video")
    veo_extend = get_generation_model("google_veo3_1_fast_video_extend")

    assert wan.input_requirements["images"]["required"] is True
    assert {"duration", "quality", "mode"} & set(wan.user_settings)
    assert kling.input_requirements["video"]["required"] is True
    assert kling.input_requirements["audio"]["required"] is True
    assert "audio" not in kling.user_settings
    assert veo_extend.input_requirements["video"]["required"] is True
    assert veo_extend.input_requirements["prompt"]["required"] is True
    assert set(veo_extend.user_settings) - {"num_generations"}


def test_media_input_fields_are_not_user_settings() -> None:
    media_setting_keys = {
        "audio",
        "audio_url",
        "input_audio",
        "video",
        "video_url",
        "input_video",
        "image",
        "image_url",
        "images",
        "image_urls",
        "input_image",
        "input_images",
        "first_frame",
        "last_frame",
        "reference_image",
        "reference_images",
        "source_image",
        "target_image",
        "face_image",
    }
    for model in list_generation_models():
        assert not (media_setting_keys & set(model.user_settings)), model.key


def test_media_fields_can_still_be_allowed_payload_fields() -> None:
    model = get_generation_model("kwaivgi_kling_lipsync_audio_to_video")

    assert {"audio", "video"} <= set(model.allowed_payload_fields)


def test_registry_logs_aggregate_params_summary_without_per_model_spam(caplog) -> None:
    caplog.set_level(logging.INFO)

    build_model_registry((get_generation_model("kwaivgi_kling_lipsync_audio_to_video"),))

    assert any("model_registry_params_summary" in record.getMessage() for record in caplog.records)
    assert not any("model_input_requirements_loaded" in record.getMessage() for record in caplog.records)


def test_build_payload_uses_collected_audio_input_url() -> None:
    payload = build_payload(
        "kwaivgi_kling_lipsync_audio_to_video",
        ["https://example.com/face.mp4"],
        "",
        {"input_audio_url": "https://example.com/voice.mp3"},
    )

    assert payload == {
        "video": "https://example.com/face.mp4",
        "audio": "https://example.com/voice.mp3",
    }


def test_runtime_merge_removes_media_fields_from_generated_user_settings() -> None:
    base_model = get_generation_model("kwaivgi_kling_lipsync_audio_to_video")

    merged_model = apply_generated_model_params(
        (base_model,),
        {
            base_model.key: {
                "allowed_payload_fields": ["video", "audio", "audio_url"],
                "required_fields": ["video", "audio"],
                "input_requirements": {
                    "prompt": {"required": False, "payload_field": "prompt"},
                    "video": {"required": True, "payload_field": "video"},
                    "audio": {"required": True, "payload_field": "audio"},
                },
                "user_settings": {
                    "audio": {"type": "string", "title": "Audio"},
                    "audio_url": {"type": "string", "title": "Audio URL"},
                    "duration": {"type": "integer", "title": "Duration", "default": "5"},
                },
            }
        },
    )[0]

    assert "audio" not in merged_model.user_settings
    assert "audio_url" not in merged_model.user_settings
    assert "duration" in merged_model.user_settings


def test_seed_is_not_exposed_for_any_enabled_model() -> None:
    for model in list_generation_models():
        assert "seed" not in model.user_settings


def test_prompt_expansion_is_not_exposed_for_any_enabled_model() -> None:
    for model in list_generation_models():
        assert "enable_prompt_expansion" not in model.user_settings


def test_negative_prompt_is_exposed_only_for_models_that_support_it() -> None:
    for model in list_generation_models():
        has_negative_prompt_setting = "negative_prompt" in model.user_settings
        supports_negative_prompt = "negative_prompt" in model.allowed_payload_fields
        assert has_negative_prompt_setting is supports_negative_prompt
        if has_negative_prompt_setting:
            assert model.user_settings["negative_prompt"].description == "Что нужно исключить из результата"


def test_openai_models_from_docs_are_enabled() -> None:
    model = get_generation_model("openai_gpt_image_1_text_to_image")

    assert model.is_enabled is True
    assert model.warning == ""
    payload = build_payload(model.key, [], "Generate a poster")
    assert payload["prompt"] == "Generate a poster"
    assert set(payload) <= set(model.allowed_payload_fields)


def test_build_payload_keeps_allowed_fields_for_known_models() -> None:
    payload = build_payload(
        "alibaba_wan_2_6_text_to_image",
        [],
        "Generate a poster",
        {"width": "1280", "height": "1536", "unknown": "ignored"},
    )

    assert payload == {"prompt": "Generate a poster", "size": "1024*1024"}


def test_build_payload_validates_generated_enum_options() -> None:
    with pytest.raises(ValueError, match="Allowed values: 8, 4, 6"):
        build_payload("google_veo3", [], "Generate a video", {"duration": "9"})


def test_build_payload_filters_unknown_and_internal_fields() -> None:
    payload = build_payload(
        "google_veo3",
        [],
        "Generate a video",
        {"duration": "4", "seed": "123", "unknown": "ignored", "num_generations": "3"},
    )

    assert payload["duration"] == "4"
    assert "seed" not in payload
    assert "unknown" not in payload
    assert "num_generations" not in payload


def test_build_payload_validates_boolean_generated_settings() -> None:
    MODEL_REGISTRY["boolean_test_model"] = GenerationModel(
        key="boolean_test_model",
        title="Boolean Test Model",
        provider="google",
        generation_type="text_to_image",
        endpoint="https://api.wavespeed.ai/api/v3/google/boolean-test-model",
        docs_url="https://wavespeed.ai/docs/docs-api/google/google-boolean-test-model",
        description="Boolean test model",
        max_images=0,
        requires_prompt=True,
        requires_image=False,
        requires_video=False,
        requires_audio=False,
        outputs="image",
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "enhance"),
        user_settings={
            "enhance": GenerationSetting(
                key="enhance",
                title="Enhance",
                type="boolean",
                default="false",
                options=(SettingOption(value="false", label="Off"), SettingOption(value="true", label="On")),
            )
        },
    )
    try:
        assert build_payload("boolean_test_model", [], "Generate", {"enhance": "true"})["enhance"] is True
        with pytest.raises(ValueError, match="Invalid boolean value"):
            build_payload("boolean_test_model", [], "Generate", {"enhance": "maybe"})
    finally:
        MODEL_REGISTRY.pop("boolean_test_model", None)


def test_empty_negative_prompt_is_not_added_to_payload() -> None:
    payload = build_payload(
        "alibaba_wan_2_6_text_to_video",
        [],
        "Generate a sunrise drone shot",
        {"negative_prompt": ""},
    )

    assert "negative_prompt" not in payload


def test_filled_negative_prompt_is_added_when_fallback_exposes_model_support() -> None:
    payload = build_payload(
        "alibaba_wan_2_6_text_to_video",
        [],
        "Generate a sunrise drone shot",
        {"negative_prompt": "blur, noise"},
    )

    assert payload["negative_prompt"] == "blur, noise"


def test_build_payload_maps_media_fields_per_model_contract() -> None:
    image_edit_payload = build_payload(
        "nano_banana",
        ["https://example.com/input.png"],
        "Refine the image",
    )
    image_to_video_payload = build_payload(
        "alibaba_wan_2_6_image_to_video_pro",
        ["https://example.com/input.png"],
        "Animate the image",
    )
    video_edit_payload = build_payload(
        "google_veo3_1_fast_video_extend",
        ["https://example.com/input.mp4"],
        "Extend the shot naturally",
    )

    assert image_edit_payload["images"] == ["https://example.com/input.png"]
    assert image_to_video_payload["image"] == "https://example.com/input.png"
    assert video_edit_payload["video"] == "https://example.com/input.mp4"


def test_build_payload_rejects_missing_required_payload_field() -> None:
    with pytest.raises(ValueError, match="Prompt must not be empty"):
        build_payload("google_veo3", [], "   ")


def test_build_payload_omits_unsupported_output_flags() -> None:
    payload = build_payload(
        "bytedance_seedream_v3_1",
        [],
        "Generate a concept art shot",
    )

    assert "enable_sync_mode" not in payload
    assert "enable_base64_output" not in payload


@pytest.mark.parametrize("model", list_generation_models(), ids=lambda model: model.key)
def test_build_payload_supports_every_enabled_model(model: GenerationModel) -> None:
    image_urls: list[str] = []
    prompt = "Smoke test prompt"
    user_settings: dict[str, str] = {}

    if model.generation_type == "lipsync":
        image_urls = ["https://example.com/face.mp4"]
        if model.requires_audio:
            payload = build_payload(model.key, [], "", {"input_video_url": image_urls[0], "input_audio_url": "https://example.com/input.mp3"})
            assert payload.get("audio") == "https://example.com/input.mp3"
        else:
            payload = build_payload(model.key, image_urls, "Lip sync text")
            assert payload.get("prompt") == "Lip sync text"
        assert payload.get("video") == "https://example.com/face.mp4"
        return

    if model.requires_image:
        image_urls = ["https://example.com/input.png"]
    elif model.requires_video:
        image_urls = ["https://example.com/input.mp4"]
    if model.requires_audio:
        user_settings["input_audio_url"] = "https://example.com/input.mp3"

    if not model.requires_prompt:
        prompt = ""

    payload = build_payload(model.key, image_urls, prompt, user_settings)

    if model.requires_prompt:
        assert payload.get("prompt") == "Smoke test prompt"
    if model.requires_image:
        image_payload_field = model.input_requirements.get("images", {}).get("payload_field", "images")
        expected_image_payload = ["https://example.com/input.png"] if model.input_media_field == "images" else "https://example.com/input.png"
        assert payload.get(image_payload_field) == expected_image_payload
    if model.requires_video:
        assert payload.get("video") == "https://example.com/input.mp4"
    if model.requires_audio:
        assert payload.get("audio") == "https://example.com/input.mp3"


def test_list_generation_types_returns_only_types_present_in_registry() -> None:
    assert list_generation_types() == [
        "text_to_image",
        "image_to_image",
        "image_edit",
        "text_to_video",
        "image_to_video",
        "reference_to_video",
        "video_edit",
        "video_extend",
        "lipsync",
        "audio_to_video",
        "video_to_audio",
    ]


def test_list_providers_returns_only_providers_with_enabled_models() -> None:
    assert list_providers() == [
        "alibaba",
        "bytedance",
        "google",
        "openai",
        "kling",
        "grok",
        "minimax",
        "wavespeed_ai",
    ]


def test_list_models_by_type_returns_only_matching_models() -> None:
    models = list_models_by_type("image_edit")

    model_keys = {model.key for model in models}
    assert {"google_nano_banana_pro_edit_ultra", "bytedance_seedream_v4_5_edit"}.issubset(model_keys)
    assert {model.key for model in list_models_by_type("text_to_image")} >= {
        "alibaba_wan_2_6_text_to_image",
        "bytedance_seedream_v3_1",
        "bytedance_seedream_v5_0_lite_sequential",
    }
    assert "openai_gpt_image_2_text_to_image" in {model.key for model in list_models_by_type("text_to_image")}


def test_list_models_by_provider_returns_only_matching_models() -> None:
    google_models = list_models_by_provider("google")
    bytedance_models = list_models_by_provider("bytedance")

    assert {"google_nano_banana_pro_edit_ultra", "google_veo3", "google_veo3_fast", "google_veo3_1_fast_video_extend"}.issubset(
        {model.key for model in google_models}
    )
    assert {"bytedance_seedream_v4_5_edit", "bytedance_seedream_v4_sequential", "bytedance_seedream_v3_1"}.issubset(
        {model.key for model in bytedance_models}
    )
    assert {model.provider for model in list_models_by_provider("openai")} == {"openai"}


def test_list_models_by_type_and_provider_returns_intersection() -> None:
    models = list_models_by_type_and_provider("image_edit", "google")

    assert "google_nano_banana_pro_edit_ultra" in {model.key for model in models}
    assert "google_imagen4" in {model.key for model in list_models_by_type_and_provider("text_to_image", "google")}


def test_build_payload_rejects_disabled_models() -> None:
    MODEL_REGISTRY["disabled_model"] = GenerationModel(
        key="disabled_model",
        title="Disabled Model",
        provider="google",
        generation_type="text_to_image",
        endpoint="https://api.wavespeed.ai/api/v3/google/disabled-model",
        docs_url="https://wavespeed.ai/docs/docs-api/google/google-disabled-model",
        description="Disabled test model",
        max_images=1,
        requires_prompt=True,
        requires_image=False,
        requires_video=False,
        requires_audio=False,
        outputs="image",
        is_enabled=False,
        warning="Endpoint needs verification",
    )
    try:
        with pytest.raises(ValueError, match="missing_docs_contract"):
            build_payload("disabled_model", [], "Test prompt")
    finally:
        MODEL_REGISTRY.pop("disabled_model", None)