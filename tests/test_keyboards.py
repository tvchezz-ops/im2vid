"""Tests for generation selection keyboards."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.keyboards import (
    build_back_to_settings_reply_keyboard,
    build_generation_confirm_keyboard,
    build_generation_sections_keyboard,
    build_model_settings_keyboard,
    build_models_keyboard,
    build_providers_keyboard,
    build_setting_options_keyboard,
    resolve_model_key_from_token,
    validate_callback_length,
)


def test_build_generation_sections_keyboard_uses_expected_callback_prefix() -> None:
    keyboard = build_generation_sections_keyboard()

    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]

    callback_data = [button.callback_data for button in buttons]
    assert "gen:section:image_edit" in callback_data
    assert "gen:section:lipsync" in callback_data
    assert "gen:all" in callback_data
    assert keyboard.inline_keyboard[-1][0].callback_data == "gen:back:main"


def test_build_providers_keyboard_uses_expected_callback_prefix() -> None:
    keyboard = build_providers_keyboard()

    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]

    callback_data = [button.callback_data for button in buttons]
    assert "gen:provider:google" in callback_data
    assert "gen:provider:bytedance" in callback_data


def test_build_models_keyboard_uses_passed_models_only() -> None:
    models = [
        SimpleNamespace(key="nano_banana", title="Nano Banana Pro Edit Ultra"),
        SimpleNamespace(key="seedream", title="Seedream V4.5 Edit"),
    ]

    keyboard = build_models_keyboard(models, "gen:back:sections")
    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]

    assert [button.text for button in buttons] == [
        "Nano Banana Pro Edit Ultra",
        "Seedream V4.5 Edit",
    ]
    assert [button.callback_data for button in buttons] == [
        "gen:model:nano_banana",
        "gen:model:seedream",
    ]


def test_build_models_keyboard_falls_back_to_index_for_long_model_key() -> None:
    long_key = "m" * 80
    models = [SimpleNamespace(key=long_key, title="Very Long Model")]

    keyboard = build_models_keyboard(models, "gen:back:sections")

    assert keyboard.inline_keyboard[0][0].callback_data == "gen:model:i0"
    assert resolve_model_key_from_token(models, "i0") == long_key


def test_validate_callback_length_rejects_64_byte_callback() -> None:
    with pytest.raises(ValueError, match="callback_data is too long"):
        validate_callback_length("x" * 64)


def test_build_model_settings_keyboard_uses_setting_keys() -> None:
    model = SimpleNamespace(
        user_settings={
            "aspect_ratio": SimpleNamespace(key="aspect_ratio", title="Формат", default="1:1"),
        }
    )

    keyboard = build_model_settings_keyboard(model, {"aspect_ratio": "16:9"})

    assert keyboard.inline_keyboard[0][0].callback_data == "gen:setting:aspect_ratio"
    assert keyboard.inline_keyboard[-1][0].callback_data == "gen:back:models"


def test_build_setting_options_keyboard_uses_setting_key_and_option_index() -> None:
    model = SimpleNamespace(
        user_settings={
            "aspect_ratio": SimpleNamespace(
                options=[
                    SimpleNamespace(value="1:1", label="1:1"),
                    SimpleNamespace(value="16:9", label="16:9"),
                ]
            )
        }
    )

    keyboard = build_setting_options_keyboard(model, "aspect_ratio", "16:9")

    assert keyboard.inline_keyboard[0][0].callback_data == "gen:set:aspect_ratio:0"
    assert keyboard.inline_keyboard[1][0].text == "✅ 16:9"
    assert keyboard.inline_keyboard[-1][0].callback_data == "gen:back:settings"


def test_build_generation_confirm_keyboard_uses_new_callbacks() -> None:
    keyboard = build_generation_confirm_keyboard()

    assert keyboard.inline_keyboard[0][0].callback_data == "gen:confirm"
    assert keyboard.inline_keyboard[1][0].callback_data == "gen:back:settings"


def test_build_back_to_settings_reply_keyboard_uses_expected_text() -> None:
    keyboard = build_back_to_settings_reply_keyboard()

    assert keyboard.keyboard[0][0].text == "⬅️ Назад к настройкам"