"""Smoke/unit tests for generation payload building."""

from __future__ import annotations

import os

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.services.generation_service import build_payload


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