"""Smoke/unit tests for generation payload building."""

from __future__ import annotations

import os
import logging
from decimal import Decimal

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.services.generation_service import (
    MODEL_REGISTRY,
    GENERATION_TYPES,
    PROVIDERS,
    GENERATION_MODELS,
    get_default_settings,
    build_model_registry,
    build_payload,
    calculate_generation_cost_credits,
    calculate_generation_price_usd,
    get_generation_model,
    GenerationModel,
    infer_generation_type_from_endpoint,
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
    is_generation_cost_estimated,
)


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
        "enable_sync_mode": False,
        "enable_base64_output": False,
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
    assert payload["enable_sync_mode"] is False
    assert payload["enable_base64_output"] is False


def test_build_payload_seedream_defaults() -> None:
    payload = build_payload(
        "seedream",
        ["https://example.com/input.png"],
        "Replace the background with a sunset cityscape",
    )

    assert payload == {
        "images": ["https://example.com/input.png"],
        "prompt": "Replace the background with a sunset cityscape",
        "size": "1024*1024",
        "enable_sync_mode": False,
        "enable_base64_output": False,
    }


def test_build_payload_seedream_custom_settings() -> None:
    payload = build_payload(
        "seedream",
        ["https://example.com/input.png"],
        "Create a cinematic poster composition",
        {"size": "2048*2048"},
    )

    assert payload["size"] == "2048*2048"
    assert payload["enable_sync_mode"] is False
    assert payload["enable_base64_output"] is False


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
        ("nano_banana", 15, "supports at most 14 images"),
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


def test_build_payload_lipsync_requires_media_and_text_or_audio() -> None:
    lipsync_model = GenerationModel(
        key="test_lipsync",
        title="Test Lipsync",
        provider="google",
        generation_type="lipsync",
          endpoint="https://example.com/lipsync",
          docs_url="https://example.com/docs/lipsync",
          description="Test lipsync model",
        max_images=1,
          requires_prompt=False,
          requires_image=False,
          requires_video=False,
          requires_audio=False,
          outputs="video",
    )
    MODEL_REGISTRY["test_lipsync"] = lipsync_model
    try:
        with pytest.raises(ValueError, match="Lipsync models require an image or video input"):
            build_payload("test_lipsync", [], "Hello there")

        with pytest.raises(ValueError, match="Lipsync models require audio or text input"):
            build_payload("test_lipsync", ["https://example.com/face.png"], "   ")
    finally:
        MODEL_REGISTRY.pop("test_lipsync", None)


def test_build_payload_lipsync_builds_media_and_text_payload() -> None:
    lipsync_model = GenerationModel(
        key="test_lipsync",
        title="Test Lipsync",
        provider="google",
        generation_type="lipsync",
          endpoint="https://example.com/lipsync",
          docs_url="https://example.com/docs/lipsync",
          description="Test lipsync model",
        max_images=1,
          requires_prompt=False,
          requires_image=False,
          requires_video=False,
          requires_audio=False,
          outputs="video",
    )
    MODEL_REGISTRY["test_lipsync"] = lipsync_model
    try:
        payload = build_payload("test_lipsync", ["https://example.com/face.png"], "Say hello")
        assert payload == {
            "image": "https://example.com/face.png",
            "text": "Say hello",
        }

        video_payload = build_payload(
            "test_lipsync",
            ["https://example.com/avatar.mp4"],
            "",
            {"audio_url": "https://example.com/voice.mp3"},
        )
        assert video_payload == {
            "video": "https://example.com/avatar.mp4",
            "audio": "https://example.com/voice.mp3",
        }
    finally:
        MODEL_REGISTRY.pop("test_lipsync", None)


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
    assert model.max_images == 14
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

    assert calculate_generation_cost_credits(model, {"duration": "5"}) == 47
    assert calculate_generation_cost_credits(model, {"duration": "10"}) == 93


def test_calculate_generation_cost_multiplies_num_generations() -> None:
    model = get_generation_model("nano_banana")

    assert calculate_generation_price_usd(model, get_default_settings(model.key), num_generations=3) == Decimal("0.630")
    assert calculate_generation_cost_credits(model, get_default_settings(model.key), num_generations=3) == 49


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


def test_build_payload_clamps_internal_num_generations_setting() -> None:
    payload = build_payload(
        "google_veo3",
        [],
        "Create a short atmospheric video",
        {"num_generations": "9", "duration": "8", "resolution": "720p", "aspect_ratio": "16:9"},
    )

    assert "num_generations" not in payload


def test_generation_registry_constants_include_supported_values() -> None:
    assert GENERATION_TYPES == [
        "text_to_image",
        "text_to_video",
        "image_edit",
        "image_to_video",
        "video_edit",
        "lipsync",
    ]
    assert PROVIDERS == [
        "alibaba",
        "openai",
        "bytedance",
        "google",
    ]

def test_required_input_type_helpers_follow_generation_type() -> None:
    assert get_required_input_type("text_to_image") == "text"
    assert get_required_input_type("text_to_video") == "text"
    assert get_required_input_type("image_edit") == "image"
    assert get_required_input_type("image_to_video") == "image"
    assert get_required_input_type("video_edit") == "video"
    assert get_required_input_type("video_to_video") == "video"
    assert get_required_input_type("lipsync") == "lipsync"

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
        ("https://wavespeed.ai/docs-api/google/image-to-image/model", "image_edit"),
        ("https://wavespeed.ai/docs-api/google/image-edit/model", "image_edit"),
        ("https://wavespeed.ai/docs-api/google/image-to-video/model", "image_to_video"),
        ("https://wavespeed.ai/docs-api/google/video-to-video/model", "video_edit"),
        ("https://wavespeed.ai/docs-api/google/video-edit/model", "video_edit"),
        ("https://wavespeed.ai/docs-api/google/lipsync/model", "lipsync"),
        ("https://wavespeed.ai/docs-api/google/talking-avatar/model", "lipsync"),
        ("https://wavespeed.ai/docs-api/google/speech-to-video/model", "lipsync"),
        ("https://wavespeed.ai/docs-api/google/voice-to-video/model", "lipsync"),
        ("https://wavespeed.ai/docs-api/google/audio-to-video/model", "lipsync"),
        ("https://wavespeed.ai/docs-api/google/video-to-video-talking-avatar/model", "lipsync"),
        ("https://wavespeed.ai/docs-api/google/unknown/model", ""),
    ],
)
def test_infer_generation_type_from_endpoint(endpoint: str, expected_generation_type: str) -> None:
    assert infer_generation_type_from_endpoint(endpoint) == expected_generation_type


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

    assert set(model.user_settings) == {"width", "height", "num_generations"}
    assert get_default_settings(model.key) == {
        "width": "1024",
        "height": "1024",
        "num_generations": "1",
    }


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


def test_unverified_openai_models_are_disabled() -> None:
    model = get_generation_model("openai_gpt_image_1_text_to_image")

    assert model.is_enabled is False
    assert model.warning == "Parameters need verification from docs"
    with pytest.raises(ValueError, match="Parameters need verification from docs"):
        build_payload(model.key, [], "Generate a poster")


def test_build_payload_keeps_allowed_fields_for_known_models() -> None:
    payload = build_payload(
        "alibaba_wan_2_6_text_to_image",
        [],
        "Generate a poster",
        {"width": "1280", "height": "1536", "unknown": "ignored"},
    )

    assert payload == {
        "prompt": "Generate a poster",
        "width": "1280",
        "height": "1536",
    }


def test_empty_negative_prompt_is_not_added_to_payload() -> None:
    payload = build_payload(
        "alibaba_wan_2_6_text_to_video",
        [],
        "Generate a sunrise drone shot",
        {"negative_prompt": ""},
    )

    assert "negative_prompt" not in payload


def test_filled_negative_prompt_is_added_to_payload() -> None:
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
        "",
    )

    assert image_edit_payload["images"] == ["https://example.com/input.png"]
    assert image_to_video_payload["image"] == "https://example.com/input.png"
    assert video_edit_payload["video"] == "https://example.com/input.mp4"


def test_build_payload_rejects_missing_required_payload_field() -> None:
    with pytest.raises(ValueError, match="Prompt must not be empty"):
        build_payload("google_veo3", [], "   ")


def test_build_payload_sets_supported_output_flags_to_false() -> None:
    payload = build_payload(
        "bytedance_seedream_v3_1",
        [],
        "Generate a concept art shot",
    )

    assert payload["enable_sync_mode"] is False
    assert payload["enable_base64_output"] is False


@pytest.mark.parametrize("model", list_generation_models(), ids=lambda model: model.key)
def test_build_payload_supports_every_enabled_model(model: GenerationModel) -> None:
    image_urls: list[str] = []
    prompt = "Smoke test prompt"
    user_settings: dict[str, str] = {}

    if model.generation_type == "lipsync":
        image_urls = ["https://example.com/face.png"]
        payload = build_payload(model.key, image_urls, "Lip sync text")
        assert payload.get("image") == "https://example.com/face.png"
        assert payload.get("text") == "Lip sync text"
        return

    if model.requires_image:
        image_urls = ["https://example.com/input.png"]
    elif model.requires_video:
        image_urls = ["https://example.com/input.mp4"]

    if not model.requires_prompt:
        prompt = ""

    payload = build_payload(model.key, image_urls, prompt, user_settings)

    if model.requires_prompt:
        assert payload.get("prompt") == "Smoke test prompt"
    if model.requires_image and model.outputs == "video":
        assert payload.get("image") == "https://example.com/input.png"
    elif model.requires_image:
        assert payload.get("images") == ["https://example.com/input.png"]
    if model.requires_video:
        assert payload.get("video") == "https://example.com/input.mp4"


def test_list_generation_types_returns_only_types_present_in_registry() -> None:
    assert list_generation_types() == [
        "text_to_image",
        "text_to_video",
        "image_edit",
        "image_to_video",
        "video_edit",
    ]


def test_list_providers_returns_only_providers_present_in_registry() -> None:
    assert list_providers() == ["alibaba", "openai", "bytedance", "google"]


def test_list_models_by_type_returns_only_matching_models() -> None:
    models = list_models_by_type("image_edit")

    model_keys = {model.key for model in models}
    assert {"google_nano_banana_pro_edit_ultra", "bytedance_seedream_v4_5_edit"}.issubset(model_keys)
    assert {model.key for model in list_models_by_type("text_to_image")} >= {
        "alibaba_wan_2_6_text_to_image",
        "bytedance_seedream_v3_1",
        "bytedance_seedream_v5_0_lite_sequential",
    }
    assert "openai_gpt_image_2_text_to_image" not in {model.key for model in list_models_by_type("text_to_image")}


def test_list_models_by_provider_returns_only_matching_models() -> None:
    google_models = list_models_by_provider("google")
    bytedance_models = list_models_by_provider("bytedance")

    assert {"google_nano_banana_pro_edit_ultra", "google_veo3", "google_veo3_fast", "google_veo3_1_fast_video_extend"}.issubset(
        {model.key for model in google_models}
    )
    assert {"bytedance_seedream_v4_5_edit", "bytedance_seedream_v4_sequential", "bytedance_seedream_v3_1"}.issubset(
        {model.key for model in bytedance_models}
    )
    assert list_models_by_provider("openai") == []


def test_list_models_by_type_and_provider_returns_intersection() -> None:
    models = list_models_by_type_and_provider("image_edit", "google")

    assert {model.key for model in models} == {"google_nano_banana_pro_edit_ultra"}
    assert list_models_by_type_and_provider("text_to_image", "google") == []


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
        with pytest.raises(ValueError, match="disabled"):
            build_payload("disabled_model", [], "Test prompt")
    finally:
        MODEL_REGISTRY.pop("disabled_model", None)