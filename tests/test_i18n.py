"""Tests for bot i18n helpers."""

from __future__ import annotations

import os


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.i18n import SUPPORTED_LANGUAGES, TRANSLATIONS, get_user_language, t


PAYMENT_TRANSLATION_KEYS = {
    "payments.top_up",
    "payments.choose_method",
    "payments.telegram_stars",
    "payments.crypto",
    "payments.choose_stars_amount",
    "payments.invoice_title",
    "payments.invoice_description",
    "payments.invoice_label",
    "payments.pre_checkout_failed",
    "payments.success",
    "payments.already_paid",
    "payments.failed",
    "payments.crypto_coming_soon",
    "payments.back_to_profile",
    "payments.open_wallet_bot",
    "payments.pay_here",
    "payments.checking_payment",
}


def test_supported_languages_match_translation_catalog() -> None:
    assert set(SUPPORTED_LANGUAGES) == set(TRANSLATIONS)


def test_all_languages_have_same_translation_keys() -> None:
    expected_keys = set(TRANSLATIONS["en"])
    for language in SUPPORTED_LANGUAGES:
        assert set(TRANSLATIONS[language]) == expected_keys


def test_all_languages_have_required_payment_translation_keys() -> None:
    for language in SUPPORTED_LANGUAGES:
        assert PAYMENT_TRANSLATION_KEYS <= set(TRANSLATIONS[language])


def test_get_user_language_returns_english_for_none() -> None:
    assert get_user_language(None) == "en"


def test_get_user_language_normalizes_regional_code() -> None:
    assert get_user_language("pt-BR") == "pt"
    assert get_user_language("zh_CN") == "zh"


def test_get_user_language_falls_back_to_english_for_unsupported_code() -> None:
    assert get_user_language("ja") == "en"


def test_translate_uses_selected_language() -> None:
    assert t("main.profile", "ru") == "Профиль"
    assert t("main.profile", "es") == "Perfil"


def test_translate_falls_back_to_english_for_missing_language_key() -> None:
    assert t("main.profile", "pt-BR") == "Perfil"
    assert t("main.profile", "ja") == "Profile"
    assert t("payments.choose_method", "ja") == "Choose a payment method:"


def test_translate_falls_back_to_english_for_missing_key() -> None:
    assert t("unknown.key", "ru") == "unknown.key"


def test_translate_formats_placeholders() -> None:
    assert t("generation.cost_label", "de", cost=12) == "Kosten: 12 Credits"