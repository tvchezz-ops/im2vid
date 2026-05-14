"""Tests for bot i18n helpers."""

from __future__ import annotations

import os
from pathlib import Path


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

import app.i18n as i18n
from app.i18n import SUPPORTED_LANGUAGES, TRANSLATIONS, get_user_language, t
from app.i18n.translations import FLOW_I18N_TRANSLATIONS


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
}

PAGINATION_TRANSLATION_KEYS = {
    "pagination.prev",
    "pagination.next",
    "pagination.page",
}

GENERATED_PARAMS_UI_KEYS = {
    "generation.send_audio",
    "generation.send_audio_description",
    "generation.audio_too_large",
    "generation.unsupported_audio_type",
    "generation.audio_received",
    "generation.prompt_optional_skipped",
    "generation.send_video_for_lipsync",
    "generation.send_audio_for_lipsync",
    "settings.enter_text_value",
    "settings.enter_number_value",
    "settings.image_size_preset_prompt",
    "settings.image_size_title",
    "settings.current_size",
    "settings.choose_aspect_ratio",
    "settings.aspect_ratio_label",
    "settings.choose_resolution",
    "settings.back_to_aspect_ratios",
    "settings.clear_hint",
    "settings.current_value",
    "settings.parameter",
    "settings.title.duration",
    "settings.title.aspect_ratio",
    "settings.title.quality",
    "settings.title.mode",
    "settings.title.negative_prompt",
    "settings.title.strength",
    "settings.title.motion_strength",
    "settings.title.resolution",
    "settings.title.num_generations",
    "settings.target_resolution",
    "settings.output_format",
    "settings.option.jpeg",
    "settings.option.png",
    "settings.option.webp",
    "requirements.image",
}

FLOW_I18N_KEYS = {
    "generation.repeat_title",
    "generation.send_image_for_model",
    "generation.send_images_for_model",
    "generation.send_video_for_model",
    "generation.send_audio_for_model",
    "generation.changed_mind_back_to_settings",
    "generation.waiting_for_image_error",
    "generation.waiting_for_video_error",
    "generation.waiting_for_audio_error",
    "generation.started_count",
    "generation.started_background",
    "generation.refund_notice",
    "settings.image_size_title",
    "settings.current_size",
    "settings.choose_aspect_ratio",
    "settings.aspect_ratio_label",
    "settings.choose_resolution",
    "settings.back_to_aspect_ratios",
    "generation.model_label",
    "generation.prompt_request",
    "generation.back_to_settings",
    "common.continue",
    "common.back_to_settings",
    "common.back",
    "common.profile",
    "common.generations",
    "errors.waiting_image",
    "errors.waiting_video",
    "errors.waiting_audio",
    "errors.text_required",
    "errors.model_unavailable",
    "errors.insufficient_balance",
    "settings.model_settings",
    "settings.choose_parameters",
    "settings.current_values",
}


def test_supported_languages_match_translation_catalog() -> None:
    assert len(SUPPORTED_LANGUAGES) == 10
    assert set(SUPPORTED_LANGUAGES) == set(TRANSLATIONS)


def test_all_languages_have_same_translation_keys() -> None:
    expected_keys = set(TRANSLATIONS["en"])
    for language in SUPPORTED_LANGUAGES:
        assert set(TRANSLATIONS[language]) == expected_keys


def test_all_languages_have_required_payment_translation_keys() -> None:
    for language in SUPPORTED_LANGUAGES:
        assert PAYMENT_TRANSLATION_KEYS <= set(TRANSLATIONS[language])


def test_all_languages_have_required_pagination_translation_keys() -> None:
    for language in SUPPORTED_LANGUAGES:
        assert PAGINATION_TRANSLATION_KEYS <= set(TRANSLATIONS[language])
        assert t("pagination.page", language, current=1, total=3)


def test_all_languages_have_generated_params_ui_translation_keys() -> None:
    for language in SUPPORTED_LANGUAGES:
        assert GENERATED_PARAMS_UI_KEYS <= set(TRANSLATIONS[language])
        assert t("generation.send_audio", language)
        assert t("settings.parameter", language, parameter="Duration")


def test_all_languages_have_generation_flow_i18n_keys() -> None:
    for language in SUPPORTED_LANGUAGES:
        assert FLOW_I18N_KEYS <= set(TRANSLATIONS[language])
        assert FLOW_I18N_KEYS <= set(FLOW_I18N_TRANSLATIONS[language])
        assert t("generation.send_image_for_model", language, model="MiniMax Image 01 Image To Image")


def test_generation_routers_do_not_embed_forbidden_user_facing_phrases() -> None:
    forbidden_phrases = (
        "Send an image",
        "If you changed your mind",
        "Back to settings",
        "Continue",
        "Model:",
    )
    repo_root = Path(__file__).resolve().parents[1]
    router_files = [
        repo_root / "app" / "bot" / "routers" / "generations.py",
        repo_root / "app" / "bot" / "routers" / "profile.py",
        repo_root / "app" / "bot" / "routers" / "payments.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in router_files)

    for phrase in forbidden_phrases:
        assert phrase not in source


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
    assert t("payments.choose_method", "ja") == "💳 Balance top-up\n\nChoose a payment method."


def test_translate_falls_back_to_english_for_missing_supported_language_key(monkeypatch) -> None:
    ru_without_payment_key = dict(TRANSLATIONS["ru"])
    ru_without_payment_key.pop("payments.choose_method")
    patched_translations = {**TRANSLATIONS, "ru": ru_without_payment_key}

    monkeypatch.setattr(i18n, "TRANSLATIONS", patched_translations)

    assert i18n.t("payments.choose_method", "ru") == "💳 Balance top-up\n\nChoose a payment method."


def test_translate_falls_back_to_english_for_missing_key() -> None:
    assert t("unknown.key", "ru") == "unknown.key"


def test_translate_formats_placeholders() -> None:
    assert t("generation.cost_label", "de", cost=12) == "Kosten: 12 Credits"