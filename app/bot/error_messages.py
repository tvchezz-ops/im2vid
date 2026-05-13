"""User-facing error UX messages and recovery keyboards."""
from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.i18n import DEFAULT_LANGUAGE, get_user_language, t
from app.utils import logger


ERROR_CODE_TO_UX_KEY = {
    "E001": "invalid_input",
    "E002": "prompt_required",
    "E003": "missing_image",
    "E004": "missing_video",
    "E005": "model_unavailable",
    "E006": "insufficient_balance",
    "E007": "generation_failed",
    "E008": "timeout",
    "E009": "delivery_failed",
    "E010": "internal",
    "E011": "invalid_settings",
    "E012": "media_prepare_failed",
}

LEGACY_ERROR_KEY_TO_UX_KEY = {
    "main.start_error": "internal",
    "main.menu_open_error": "internal",
    "errors.internal": "internal",
    "errors.internal_retry": "internal",
    "errors.request_processing_failed": "internal",
    "errors.user_not_found": "internal",
    "profile.user_not_found": "internal",
    "profile.load_error": "internal",
    "payments.invalid_amount": "invalid_settings",
    "payments.invoice_error": "internal",
    "payments.failed": "internal",
    "payments.already_paid": "payment_already_processed",
    "payments.stars_wallet_not_configured": "payment_unavailable",
    "payments.crypto_not_configured": "payment_unavailable",
    "generation.insufficient_balance": "insufficient_balance",
    "generation.insufficient_balance_start": "insufficient_balance",
    "errors.insufficient_balance": "insufficient_balance",
    "errors.insufficient_balance_details": "insufficient_balance",
    "errors.launch_generation_failed": "internal",
    "errors.finish_generation_failed": "internal",
    "errors.model_unavailable": "model_unavailable",
    "errors.model_contract_unavailable": "model_unavailable",
    "generation.model_not_selected": "model_unavailable",
    "generation.provider_unavailable": "model_unavailable",
    "generation.no_models_in_section": "no_models",
    "generation.no_models_in_provider": "no_models",
    "generation.setting_not_found": "invalid_settings",
    "errors.setting_not_selected": "invalid_settings",
    "errors.setting_unavailable": "invalid_settings",
    "errors.setting_text_required": "prompt_required",
    "generation.invalid_value": "invalid_settings",
    "errors.invalid_model_settings": "invalid_settings",
    "generation.legacy_button": "internal",
    "errors.prompt_text_only": "prompt_required",
    "errors.invalid_input_generic": "invalid_input",
    "errors.incomplete_generation": "internal",
    "errors.short_description": "prompt_required",
    "errors.text_required": "prompt_required",
    "generation.need_min_images": "missing_image",
    "generation.image_limit_reached": "invalid_settings",
    "generation.invalid_wait_image": "missing_image",
    "generation.invalid_wait_video": "missing_video",
    "generation.invalid_wait_lipsync": "invalid_input",
    "generation.waiting_for_image_error": "missing_image",
    "generation.waiting_for_video_error": "missing_video",
    "generation.waiting_for_audio_error": "missing_audio",
    "errors.waiting_image": "missing_image",
    "errors.waiting_video": "missing_video",
    "errors.waiting_audio": "missing_audio",
    "generation.lipsync_incomplete": "invalid_input",
    "generation.lipsync_need_text": "prompt_required",
    "generation.lipsync_need_media": "invalid_input",
    "generation.lipsync_need_face_media": "invalid_input",
    "generation.unsupported_audio_type": "missing_audio",
    "generation.audio_too_large": "audio_too_large",
    "errors.prepare_image_failed": "media_prepare_failed",
    "errors.prepare_video_failed": "media_prepare_failed",
    "errors.prepare_media_failed": "media_prepare_failed",
    "errors.generation_failed_refund": "generation_failed",
    "errors.partial_generation_failed": "generation_failed",
    "errors.timeout_refund": "timeout",
    "errors.rejected_by_provider": "generation_failed",
    "errors.telegram_delivery_failed": "delivery_failed",
    "errors.telegram_delivery_failed_refund": "delivery_failed",
    "errors.generated_delivery_failed_refund": "delivery_failed",
    "errors.empty_outputs_failed_refund": "delivery_failed",
    "errors.result_network_failure": "delivery_failed",
}

UX_KEYBOARD_GROUPS = {
    "insufficient_balance": "balance",
    "model_unavailable": "generations",
    "no_models": "generations",
    "invalid_settings": "settings",
    "generation_failed": "retry_generation",
    "timeout": "retry_generation",
    "delivery_failed": "retry_generation",
    "internal": "main_menu",
}


def resolve_error_ux_key(error_code: str) -> str:
    return ERROR_CODE_TO_UX_KEY.get(error_code, LEGACY_ERROR_KEY_TO_UX_KEY.get(error_code, error_code))


def build_user_error_message(error_code: str, lang: str = DEFAULT_LANGUAGE, **kwargs: Any) -> str:
    """Build a localized user-facing error message without exposing internal codes."""
    resolved_lang = get_user_language(lang)
    ux_key = resolve_error_ux_key(error_code)
    return t(f"error_ux.{ux_key}", resolved_lang, **kwargs)


def build_error_keyboard(error_code: str, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup | None:
    """Build recovery buttons for an error UX message when the flow has a clear action."""
    resolved_lang = get_user_language(lang)
    ux_key = resolve_error_ux_key(error_code)
    group = UX_KEYBOARD_GROUPS.get(ux_key)
    if group == "balance":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("error_ux.button.top_up", resolved_lang), callback_data="profile:topup")],
                [InlineKeyboardButton(text=t("error_ux.button.profile", resolved_lang), callback_data="profile:open")],
            ]
        )
    if group == "generations":
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t("error_ux.button.generations", resolved_lang), callback_data="gen:retry")]]
        )
    if group == "settings":
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t("error_ux.button.settings", resolved_lang), callback_data="gen:back:settings")]]
        )
    if group == "retry_generation":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("error_ux.button.try_again", resolved_lang), callback_data="gen:retry")],
                [InlineKeyboardButton(text=t("error_ux.button.generations", resolved_lang), callback_data="gen:retry")],
            ]
        )
    if group == "main_menu":
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t("error_ux.button.main_menu", resolved_lang), callback_data="back_to_menu")]]
        )
    return None


def log_error_code(error_code: str, details: dict[str, Any]) -> None:
    """Log internal error metadata while keeping the UI code-free."""
    logger.error({"error_code": error_code, **details})
