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
    build_main_menu_keyboard,
    build_model_settings_keyboard,
    build_models_keyboard,
    build_providers_keyboard,
    build_crypto_top_up_keyboard,
    build_setting_options_keyboard,
    build_stars_wallet_redirect_keyboard,
    build_stars_top_up_keyboard,
    build_top_up_method_keyboard,
    build_wallet_bot_payment_keyboard,
    resolve_model_key_from_token,
    validate_callback_length,
)


def test_build_generation_sections_keyboard_uses_expected_callback_prefix() -> None:
    keyboard = build_generation_sections_keyboard()

    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]
    all_button = keyboard.inline_keyboard[-1][0]

    callback_data = [button.callback_data for button in buttons]
    assert "gen:section:image_edit" in callback_data
    assert "gen:section:text_to_video" in callback_data
    assert "gen:section:lipsync" not in callback_data
    assert all_button.callback_data == "gen:all"


def test_build_providers_keyboard_uses_expected_callback_prefix() -> None:
    keyboard = build_providers_keyboard()

    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]

    callback_data = [button.callback_data for button in buttons]
    assert "gen:provider:google" in callback_data
    assert "gen:provider:bytedance" in callback_data
    assert "gen:provider:midjourney" not in callback_data


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
    keyboard = build_back_to_settings_reply_keyboard("ru")

    assert keyboard.keyboard[0][0].text == "⬅️ Назад к настройкам"


def test_build_main_menu_keyboard_uses_expected_layout() -> None:
    keyboard = build_main_menu_keyboard("ru")

    assert keyboard.keyboard[0][0].text == "🎨 Генерации"
    assert keyboard.keyboard[0][1].text == "👤 Профиль"
    assert len(keyboard.keyboard) == 1
    assert all(button.text != "🛒 Магазин" for row in keyboard.keyboard for button in row)
    assert keyboard.resize_keyboard is True
    assert keyboard.one_time_keyboard is False
    assert keyboard.input_field_placeholder == "Выберите раздел"


def test_build_profile_keyboard_has_no_history_button() -> None:
    from app.bot.keyboards import get_profile_keyboard

    keyboard = get_profile_keyboard(send_results_as_files=False, lang="ru")
    button_texts = [button.text for row in keyboard.inline_keyboard for button in row]

    assert button_texts == ["💳 Пополнить баланс", "📎 Переключить способ отправки"]
    assert "📜 История генераций" not in button_texts


def test_build_profile_keyboard_has_no_back_button_in_ru_or_en() -> None:
    from app.bot.keyboards import get_profile_keyboard

    ru_keyboard = get_profile_keyboard(send_results_as_files=False, lang="ru")
    en_keyboard = get_profile_keyboard(send_results_as_files=False, lang="en")
    ru_button_texts = [button.text for row in ru_keyboard.inline_keyboard for button in row]
    en_button_texts = [button.text for row in en_keyboard.inline_keyboard for button in row]

    assert "Назад" not in " ".join(ru_button_texts)
    assert "Back" not in " ".join(en_button_texts)


def test_build_stars_top_up_keyboard_uses_expected_amount_callbacks() -> None:
    keyboard = build_stars_top_up_keyboard("ru")
    rows = keyboard.inline_keyboard
    buttons = [button for row in rows for button in row]

    assert [button.text for button in buttons] == [
        "100 ⭐",
        "300 ⭐",
        "500 ⭐",
        "1000 ⭐",
        "3000 ⭐",
        "5000 ⭐",
        "⬅️ Назад",
    ]
    assert [button.callback_data for button in buttons] == [
        "pay:stars:100",
        "pay:stars:300",
        "pay:stars:500",
        "pay:stars:1000",
        "pay:stars:3000",
        "pay:stars:5000",
        "pay:back:methods",
    ]
    assert all("Магазин" not in button.text for button in buttons)


def test_build_stars_top_up_keyboard_uses_two_column_amount_layout() -> None:
    keyboard = build_stars_top_up_keyboard("ru")
    rows = keyboard.inline_keyboard

    assert len(rows) == 4
    assert [len(row) for row in rows[:3]] == [2, 2, 2]
    assert [button.text for row in rows[:3] for button in row] == [
        "100 ⭐",
        "300 ⭐",
        "500 ⭐",
        "1000 ⭐",
        "3000 ⭐",
        "5000 ⭐",
    ]
    assert len(rows[-1]) == 1
    assert rows[-1][0].text == "⬅️ Назад"
    assert rows[-1][0].callback_data == "pay:back:methods"


def test_build_crypto_top_up_keyboard_uses_two_column_amount_layout() -> None:
    keyboard = build_crypto_top_up_keyboard("ru")
    rows = keyboard.inline_keyboard

    assert len(rows) == 4
    assert [len(row) for row in rows[:3]] == [2, 2, 2]
    assert [button.text for row in rows[:3] for button in row] == [
        "100 credits",
        "300 credits",
        "500 credits",
        "1000 credits",
        "3000 credits",
        "5000 credits",
    ]
    assert [button.callback_data for row in rows[:3] for button in row] == [
        "pay:crypto:100",
        "pay:crypto:300",
        "pay:crypto:500",
        "pay:crypto:1000",
        "pay:crypto:3000",
        "pay:crypto:5000",
    ]
    assert len(rows[-1]) == 1
    assert rows[-1][0].text == "⬅️ Назад"
    assert rows[-1][0].callback_data == "pay:back:methods"


def test_build_top_up_method_keyboard_uses_payment_methods() -> None:
    keyboard = build_top_up_method_keyboard("ru")
    buttons = [row[0] for row in keyboard.inline_keyboard]

    assert [button.text for button in buttons] == ["⭐ Telegram Stars", "₿ Crypto", "⬅️ Назад в профиль"]
    assert [button.callback_data for button in buttons] == ["pay:method:stars", "pay:crypto", "pay:back:profile"]


def test_build_stars_wallet_redirect_keyboard_uses_single_wallet_url() -> None:
    keyboard = build_stars_wallet_redirect_keyboard(
        wallet_payment_url="https://t.me/wallet_bot?start=stars_token",
        lang="ru",
    )

    assert keyboard.inline_keyboard[0][0].text == "Перейти к оплате ⭐"
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/wallet_bot?start=stars_token"
    assert keyboard.inline_keyboard[1][0].text == "⬅️ Назад"
    assert keyboard.inline_keyboard[1][0].callback_data == "pay:back:stars_amounts"


def test_build_wallet_bot_payment_keyboard_uses_single_pay_url_button() -> None:
    keyboard = build_wallet_bot_payment_keyboard(
        amount=500,
        wallet_payment_url="https://t.me/wallet_bot?start=500credits",
    )

    assert len(keyboard.inline_keyboard) == 1
    assert keyboard.inline_keyboard[0][0].text == "Go to payment"
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/wallet_bot?start=500credits"
