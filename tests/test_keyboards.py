"""Tests for generation selection keyboards."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.keyboards import (
    build_generation_type_keyboard,
    build_model_selection_keyboard,
    build_provider_keyboard,
)


def test_build_generation_type_keyboard_uses_expected_callback_prefix() -> None:
    keyboard = build_generation_type_keyboard([
        ("image_to_image", "🧩 Image → Image"),
        ("lipsync", "🗣 Lipsync"),
        ("all", "📚 All models"),
    ])

    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]

    assert [button.callback_data for button in buttons] == [
        "gen:type:image_to_image",
        "gen:type:lipsync",
        "gen:type:all",
    ]


def test_build_provider_keyboard_uses_expected_callback_prefix() -> None:
    keyboard = build_provider_keyboard([
        ("google", "Google"),
        ("bytedance", "ByteDance"),
    ])

    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]

    assert [button.callback_data for button in buttons] == [
        "gen:provider:google",
        "gen:provider:bytedance",
    ]


def test_build_model_selection_keyboard_uses_passed_models_only() -> None:
    models = [
        SimpleNamespace(key="nano_banana", title="Nano Banana Pro Edit Ultra"),
        SimpleNamespace(key="seedream", title="Seedream V4.5 Edit"),
    ]

    keyboard = build_model_selection_keyboard(models)
    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]

    assert [button.text for button in buttons] == [
        "Nano Banana Pro Edit Ultra",
        "Seedream V4.5 Edit",
    ]
    assert [button.callback_data for button in buttons] == [
        "gen:model:nano_banana",
        "gen:model:seedream",
    ]


def test_build_model_selection_keyboard_rejects_too_long_callback_data() -> None:
    long_key = "m" * 60
    models = [SimpleNamespace(key=long_key, title="Very Long Model")]

    with pytest.raises(ValueError, match="callback_data is too long"):
        build_model_selection_keyboard(models)