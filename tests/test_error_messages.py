from __future__ import annotations

import logging
import os
import re

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.bot.error_messages import build_error_keyboard, build_user_error_message, log_error_code
from app.i18n import SUPPORTED_LANGUAGES, TRANSLATIONS


ERROR_CODES = tuple(f"E{number:03d}" for number in range(1, 13))
ERROR_CODE_RE = re.compile(r"\bE00[1-9]\b|\bE01[0-2]\b")
ERROR_UX_KEYS = {
    "error_ux.invalid_input",
    "error_ux.prompt_required",
    "error_ux.missing_image",
    "error_ux.missing_video",
    "error_ux.model_unavailable",
    "error_ux.insufficient_balance",
    "error_ux.generation_failed",
    "error_ux.timeout",
    "error_ux.delivery_failed",
    "error_ux.internal",
    "error_ux.invalid_settings",
    "error_ux.media_prepare_failed",
    "error_ux.missing_audio",
    "error_ux.audio_too_large",
    "error_ux.payment_unavailable",
    "error_ux.payment_already_processed",
    "error_ux.no_models",
    "error_ux.button.top_up",
    "error_ux.button.profile",
    "error_ux.button.generations",
    "error_ux.button.settings",
    "error_ux.button.try_again",
    "error_ux.button.main_menu",
    "wallet.error.invalid_payment_link",
    "wallet.error.payment_order_not_found",
}


def flatten_keyboard_texts(keyboard) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def test_error_ux_messages_do_not_contain_internal_error_codes() -> None:
    for language in SUPPORTED_LANGUAGES:
        for error_code in ERROR_CODES:
            message = build_user_error_message(error_code, language, balance=10)
            assert ERROR_CODE_RE.search(message) is None


def test_error_ux_logs_keep_error_code(caplog) -> None:
    caplog.set_level(logging.ERROR)

    log_error_code("E008", {"action": "test_timeout"})

    assert any(isinstance(record.msg, dict) and record.msg.get("error_code") == "E008" for record in caplog.records)


def test_e006_maps_to_balance_message_and_recovery_buttons() -> None:
    message = build_user_error_message("E006", "ru")
    keyboard = build_error_keyboard("E006", "ru")

    assert message == "💳 Недостаточно кредитов\n\nПополните баланс в профиле, чтобы продолжить."
    assert keyboard is not None
    assert flatten_keyboard_texts(keyboard) == ["💳 Пополнить баланс", "👤 Профиль"]
    assert [button.callback_data for row in keyboard.inline_keyboard for button in row] == ["profile:topup", "profile:open"]


def test_e008_timeout_message_has_no_internal_code() -> None:
    message = build_user_error_message("E008", "en")

    assert message == "⏱️ Generation took too long\n\nCredits for this attempt were returned. Please try again later."
    assert "E008" not in message


def test_ru_and_en_error_messages_do_not_mix_languages() -> None:
    ru_message = build_user_error_message("E006", "ru")
    en_message = build_user_error_message("E006", "en")

    assert "Недостаточно кредитов" in ru_message
    assert "Not enough credits" not in ru_message
    assert "Not enough credits" in en_message
    assert "Недостаточно кредитов" not in en_message


def test_every_error_ux_key_exists_for_all_supported_languages() -> None:
    for language in SUPPORTED_LANGUAGES:
        assert ERROR_UX_KEYS <= set(TRANSLATIONS[language])
