from __future__ import annotations

import logging
import os
import re

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.bot.error_messages import build_error_keyboard, build_user_error_message, log_error_code
from app.i18n import SUPPORTED_LANGUAGES, TRANSLATIONS, t


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

EXPECTED_ERROR_MESSAGES = {
    "en": {
        "E001": "📎 Unsupported file type\n\nThis model does not support the uploaded format.\nPlease send the correct image, video, or audio file for this model.",
        "E002": "✍️ Description required\n\nPlease add a text description of the result you want to generate.\nGeneration cannot start without it.",
        "E003": "🖼 Image required\n\nThis model requires an image to continue.\nPlease upload a photo or image file.",
        "E004": "🎬 Video required\n\nThis model requires a video file.\nPlease upload a video and try again.",
        "E005": "⚙️ Model temporarily unavailable\n\nThis model is currently unavailable or still being configured.\nPlease choose another model and try again.",
        "E006": "💳 Not enough credits\n\nYour balance is too low to start this generation.\nOpen Profile → Top Up Balance and try again.",
        "E007": "❌ Failed to generate result\n\nThe generation server returned an error while processing your request.\nPlease try again later.\nIf the generation was not completed, your credits will be refunded automatically.",
        "E008": "⏳ Generation took too long\n\nThe server could not finish processing in time.\nPlease try again later — your credits were refunded.",
        "E009": "📤 Failed to deliver result\n\nThe result was generated, but Telegram could not deliver the file.\nYour credits were refunded automatically.",
        "E010": "⚠️ Internal error\n\nSomething went wrong while processing your request.\nPlease try again in a few seconds.",
        "E011": "🛠 Invalid model settings\n\nSome model parameters were filled incorrectly.\nPlease check your settings and try again.",
        "E012": "☁️ Media upload failed\n\nThe media file could not be processed or uploaded.\nPlease try uploading the file again or use another file.",
    },
    "ru": {
        "E001": "📎 Неверный тип файла\n\nЭта модель не поддерживает такой формат.\nПожалуйста, отправьте подходящий файл: изображение, видео или аудио — в зависимости от выбранной модели.",
        "E002": "✍️ Нужно описание\n\nДобавьте текстовое описание того, что хотите получить.\nБез описания генерацию запустить нельзя.",
        "E003": "🖼 Нужно изображение\n\nДля этой модели требуется фото или изображение.\nОтправьте картинку и попробуйте снова.",
        "E004": "🎬 Нужно видео\n\nДля этой модели необходимо загрузить видео.\nОтправьте видеофайл и повторите попытку.",
        "E005": "⚙️ Модель временно недоступна\n\nЭта модель сейчас недоступна или ещё настраивается.\nВыберите другую модель и попробуйте снова.",
        "E006": "💳 Недостаточно кредитов\n\nНа балансе недостаточно кредитов для запуска генерации.\nОткройте Профиль → Пополнить баланс и попробуйте снова.",
        "E007": "❌ Не удалось создать результат\n\nВо время генерации произошла ошибка со стороны сервера.\nПопробуйте ещё раз немного позже.\nЕсли генерация не была завершена — кредиты автоматически вернутся.",
        "E008": "⏳ Генерация заняла слишком много времени\n\nСервер не успел завершить обработку вовремя.\nПопробуйте ещё раз позже — кредиты возвращены.",
        "E009": "📤 Не удалось отправить результат\n\nРезультат был создан, но Telegram не смог доставить файл.\nКредиты возвращены автоматически.",
        "E010": "⚠️ Внутренняя ошибка\n\nВо время обработки произошёл сбой.\nПопробуйте снова через несколько секунд.",
        "E011": "🛠 Некорректные настройки\n\nНекоторые параметры модели заполнены неверно.\nПроверьте настройки генерации и попробуйте снова.",
        "E012": "☁️ Ошибка загрузки файла\n\nНе удалось обработать или загрузить медиафайл.\nПопробуйте отправить файл ещё раз или используйте другой файл.",
    },
}


def flatten_keyboard_texts(keyboard) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def test_error_ux_messages_do_not_contain_internal_error_codes() -> None:
    for language in SUPPORTED_LANGUAGES:
        for error_code in ERROR_CODES:
            message = build_user_error_message(error_code, language, balance=10)
            assert ERROR_CODE_RE.search(message) is None


def test_ru_and_en_generation_error_messages_match_requested_copy() -> None:
    for language, expected_messages in EXPECTED_ERROR_MESSAGES.items():
        for error_code, expected_message in expected_messages.items():
            assert build_user_error_message(error_code, language) == expected_message


def test_error_ux_logs_keep_error_code(caplog) -> None:
    caplog.set_level(logging.ERROR)

    log_error_code("E008", {"action": "test_timeout"})

    assert any(isinstance(record.msg, dict) and record.msg.get("error_code") == "E008" for record in caplog.records)


def test_e006_maps_to_balance_message_and_recovery_buttons() -> None:
    message = build_user_error_message("E006", "ru")
    keyboard = build_error_keyboard("E006", "ru")

    assert message == t("error_ux.insufficient_balance", "ru")
    assert keyboard is not None
    assert flatten_keyboard_texts(keyboard) == ["💳 Пополнить", "👤 Профиль"]
    assert [button.callback_data for row in keyboard.inline_keyboard for button in row] == ["profile:topup", "profile:open"]


def test_e008_timeout_message_has_no_internal_code() -> None:
    message = build_user_error_message("E008", "en")

    assert message == t("error_ux.timeout", "en")
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


def test_error_ux_messages_include_explanation_and_next_step() -> None:
    error_codes = ("E001", "E002", "E003", "E004", "E005", "E006", "E007", "E008", "E009", "E010", "E011", "E012")

    for language in SUPPORTED_LANGUAGES:
        for error_code in error_codes:
            message = build_user_error_message(error_code, language)
            paragraphs = [part for part in message.split("\n\n") if part.strip()]

            assert len(paragraphs) >= 2
            assert "\n" in paragraphs[-1]


def test_refund_errors_explain_credits_are_returned() -> None:
    assert "credits will be refunded automatically" in build_user_error_message("E007", "en")
    assert "credits were refunded" in build_user_error_message("E008", "en")
    assert "credits were refunded automatically" in build_user_error_message("E009", "en")
    assert "кредиты автоматически вернутся" in build_user_error_message("E007", "ru")
    assert "кредиты возвращены" in build_user_error_message("E008", "ru")
    assert "Кредиты возвращены автоматически" in build_user_error_message("E009", "ru")


def test_delivery_error_explains_telegram_delivery_issue() -> None:
    en_message = build_user_error_message("E009", "en")
    ru_message = build_user_error_message("E009", "ru")

    assert "Telegram could not deliver the file" in en_message
    assert "Telegram не смог доставить файл" in ru_message


def test_ru_and_en_error_messages_keep_language_boundaries() -> None:
    ru_message = build_user_error_message("E007", "ru")
    en_message = build_user_error_message("E007", "en")

    assert "Failed to generate result" not in ru_message
    assert "Please try again later" not in ru_message
    assert "Не удалось создать результат" not in en_message
    assert "кредиты автоматически" not in en_message
