"""Smoke/unit tests for generation payload building."""

from __future__ import annotations

import os

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.services.generation_service import (
    MODEL_REGISTRY,
    GENERATION_TYPES,
    PROVIDERS,
    GENERATION_MODELS,
    build_model_registry,
    build_payload,
    get_generation_model,
    GenerationModel,
    infer_generation_type_from_endpoint,
    list_generation_models,
    list_generation_types,
    list_models_by_provider,
    list_models_by_type,
    list_models_by_type_and_provider,
    list_providers,
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
    with pytest.raises(ValueError, match="All image URLs must be string values"):
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
        endpoint="https://example.com/lipsync",
        provider="google",
        generation_type="lipsync",
        max_images=1,
        required_fields=("image", "text"),
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
        endpoint="https://example.com/lipsync",
        provider="google",
        generation_type="lipsync",
        max_images=1,
        required_fields=("image", "text"),
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

    assert model.provider == "google"
    assert model.generation_type == "image_to_image"
    assert model.type == "image_to_image"
    assert model.model_type == "image_to_image"


def test_generation_registry_constants_include_supported_values() -> None:
    assert GENERATION_TYPES == [
        "text_to_image",
        "text_to_video",
        "image_to_image",
        "image_to_video",
        "video_to_video",
        "lipsync",
    ]
    assert PROVIDERS == [
        "alibaba",
        "openai",
        "bytedance",
        "google",
        "midjourney",
    ]


def test_model_registry_is_canonical_and_compatible() -> None:
    assert MODEL_REGISTRY is GENERATION_MODELS
    assert MODEL_REGISTRY["nano_banana"].provider == "google"
    assert MODEL_REGISTRY["seedream"].provider == "bytedance"
    assert MODEL_REGISTRY["seedream"].generation_type == "image_to_image"


def test_build_model_registry_rejects_invalid_metadata() -> None:
    with pytest.raises(ValueError, match="Unsupported provider 'unknown'"):
        build_model_registry((
            GenerationModel(
                key="broken",
                title="Broken",
                endpoint="https://example.com/broken",
                provider="unknown",
                generation_type="image_to_image",
                max_images=1,
                required_fields=("prompt",),
            ),
        ))


@pytest.mark.parametrize(
    ("endpoint", "expected_generation_type"),
    [
        ("https://wavespeed.ai/docs-api/google/text-to-image/model", "text_to_image"),
        ("https://wavespeed.ai/docs-api/google/text-to-video/model", "text_to_video"),
        ("https://wavespeed.ai/docs-api/google/image-to-image/model", "image_to_image"),
        ("https://wavespeed.ai/docs-api/google/image-to-video/model", "image_to_video"),
        ("https://wavespeed.ai/docs-api/google/video-to-video/model", "video_to_video"),
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


def test_build_model_registry_infers_generation_type_from_endpoint() -> None:
    registry = build_model_registry((
        GenerationModel(
            key="inferred-model",
            title="Inferred Model",
            endpoint="https://wavespeed.ai/docs-api/google/text-to-image/inferred-model",
            provider="google",
            generation_type="",
            max_images=1,
            required_fields=("prompt",),
        ),
    ))

    assert registry["inferred-model"].generation_type == "text_to_image"


def test_build_model_registry_ignores_model_when_generation_type_cannot_be_inferred() -> None:
    registry = build_model_registry((
        GenerationModel(
            key="ignored-model",
            title="Ignored Model",
            endpoint="https://wavespeed.ai/docs-api/google/custom-endpoint/ignored-model",
            provider="google",
            generation_type="",
            max_images=1,
            required_fields=("prompt",),
        ),
    ))

    assert registry == {}


def test_list_generation_models_returns_all_registry_models() -> None:
    assert list_generation_models() == list(MODEL_REGISTRY.values())


def test_list_generation_types_returns_only_types_present_in_registry() -> None:
    assert list_generation_types() == ["image_to_image"]


def test_list_providers_returns_only_providers_present_in_registry() -> None:
    assert list_providers() == ["bytedance", "google"]


def test_list_models_by_type_returns_only_matching_models() -> None:
    models = list_models_by_type("image_to_image")

    assert [model.key for model in models] == ["nano_banana", "seedream"]
    assert list_models_by_type("text_to_image") == []


def test_list_models_by_provider_returns_only_matching_models() -> None:
    google_models = list_models_by_provider("google")
    bytedance_models = list_models_by_provider("bytedance")

    assert [model.key for model in google_models] == ["nano_banana"]
    assert [model.key for model in bytedance_models] == ["seedream"]
    assert list_models_by_provider("openai") == []


def test_list_models_by_type_and_provider_returns_intersection() -> None:
    models = list_models_by_type_and_provider("image_to_image", "google")

    assert [model.key for model in models] == ["nano_banana"]
    assert list_models_by_type_and_provider("text_to_image", "google") == []