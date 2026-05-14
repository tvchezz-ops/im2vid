"""Роутер генерации контента."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, Mapping, Optional
from urllib.parse import unquote, urlparse
import uuid

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message, ReplyKeyboardRemove
import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import (
    build_back_to_settings_keyboard,
    build_insufficient_balance_keyboard,
    build_media_upload_reply_keyboard,
    build_generation_confirm_keyboard,
    build_generation_sections_keyboard,
    build_generation_summary_keyboard,
    build_generation_type_keyboard,
    build_models_keyboard,
    build_model_selection_keyboard,
    build_model_settings_keyboard,
    build_providers_keyboard,
    build_provider_keyboard,
    build_setting_input_back_keyboard,
    build_setting_options_keyboard,
    get_main_menu_keyboard,
    is_localized_button_text,
    PAGINATION_NOOP_CALLBACK,
    resolve_model_key_from_token,
)
from app.bot.error_messages import build_error_keyboard, build_user_error_message, log_error_code
from app.bot.language import get_event_lang
from app.bot.states import GenerationStates
from app.config import settings
from app.db import GenerationRepository, GenerationRequestStatus, UserRepository
from app.db.session import db_manager
from app.i18n import DEFAULT_LANGUAGE, get_user_language, t
from app.services.generation_service import (
    GENERATION_CATEGORIES,
    GenerationModel,
    allocate_generation_cost_credits,
    build_payload,
    calculate_generation_cost_credits,
    calculate_generation_price_quote,
    generation_cost_has_minimum_label,
    get_default_settings,
    get_generation_model,
    get_model_num_generations,
    get_model_required_input_type,
    is_generation_cost_estimated,
    list_generation_types,
    list_generation_models,
    list_models_by_provider,
    list_models_by_type,
    model_requires_media,
    validate_model_settings,
    list_providers,
)
from app.services.download_links import DownloadLinkService
from app.services.r2_storage import R2StorageService
from app.services.telegram_files import TelegramFilesService
from app.services.wavespeed import WavespeedResult, WavespeedService
from app.utils import (
    ImageUploadError,
    WavespeedFailedError,
    WavespeedNetworkError,
    WavespeedTimeoutError,
    logger,
    sanitize_external_error_message,
)


router = Router()


def get_actor_language(actor: Any) -> str:
    return get_user_language(getattr(actor, "language_code", None))


def get_user_preferred_language(user: Any | None = None, actor: Any | None = None) -> str:
    if user is not None and getattr(user, "language_code", None):
        return get_user_language(getattr(user, "language_code", None))
    return get_actor_language(actor)


def get_state_language(state_data: dict[str, Any], actor: Any | None = None, user: Any | None = None) -> str:
    if user is not None:
        return get_user_preferred_language(user)
    if state_data.get("user_language"):
        return get_user_language(str(state_data.get("user_language")))
    return get_actor_language(actor)

BACKGROUND_GENERATIONS: Dict[Any, Dict[str, Any]] = {}
DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS = 3600
DOCUMENT_SEND_RETRY_COUNT = 3
OUTPUT_DOWNLOAD_TIMEOUT_SECONDS = 300
MEDIA_GROUP_DEBOUNCE_SECONDS = 1.0
AUDIO_MAX_SIZE_MB = 20
AUDIO_FILE_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac"}
MEDIA_GROUP_BUFFERS: dict[str, list[Message]] = {}
MEDIA_GROUP_STATES: dict[str, FSMContext] = {}
MEDIA_GROUP_TASKS: dict[str, asyncio.Task] = {}
MEDIA_GROUP_MODES: dict[str, str] = {}

GENERATION_FLOW_STATE_NAMES = {
    GenerationStates.choosing_generation_type.state,
    GenerationStates.choosing_provider.state,
    GenerationStates.choosing_settings.state,
    GenerationStates.choosing_setting_value.state,
    GenerationStates.waiting_for_setting_text.state,
    GenerationStates.waiting_for_setting_number.state,
    GenerationStates.waiting_for_images.state,
    GenerationStates.waiting_for_image.state,
    GenerationStates.waiting_for_video.state,
    GenerationStates.waiting_for_prompt.state,
    GenerationStates.waiting_for_confirmation.state,
}

MODEL_PREFIX = "gen:model:"
MODELS_PAGE_PREFIX = "gen:models:"
GENERATION_SECTION_PREFIX = "gen:section:"
GENERATION_ALL = "gen:all"
ALL_MODELS_CATEGORY = "all_models"
LEGACY_ALL_MODELS_CATEGORY = "all"
PROVIDER_PREFIX = "gen:provider:"
PROVIDERS_PAGE_PREFIX = "gen:providers:"
SETTINGS_OPEN_PREFIX = "gen:setting:"
SETTINGS_VALUE_PREFIX = "gen:set:"
BACK_TO_SECTIONS = "gen:back:sections"
BACK_TO_PROVIDERS = "gen:back:providers"
SETTINGS_BACK_PREFIX = "gen:back:settings"
SETTINGS_BACK_MODELS = "gen:back:models"
SETTINGS_CONTINUE = "gen:continue"
GENERATION_CONFIRM = "gen:confirm"
GENERATION_REPEAT_PREFIX = "gen:repeat:"


def parse_page(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def parse_provider_page(callback_data: str) -> tuple[str, int]:
    payload = callback_data.removeprefix(PROVIDER_PREFIX)
    provider, separator, page = payload.rpartition(":")
    if separator and page.isdigit():
        return provider, parse_page(page)
    return payload, 0


def parse_models_page(callback_data: str) -> tuple[str, int]:
    payload = callback_data.removeprefix(MODELS_PAGE_PREFIX)
    generation_type, separator, page = payload.rpartition(":")
    if separator and page.isdigit():
        return generation_type, parse_page(page)
    return payload, 0


class ErrorCode:
    E001_INVALID_INPUT_TYPE = "E001"
    E002_MISSING_PROMPT = "E002"
    E003_MISSING_IMAGE = "E003"
    E004_MISSING_VIDEO = "E004"
    E005_UNSUPPORTED_MODEL = "E005"
    E006_INSUFFICIENT_BALANCE = "E006"
    E007_WAVESPEED_FAILED = "E007"
    E008_WAVESPEED_TIMEOUT = "E008"
    E009_TELEGRAM_DELIVERY_FAILED = "E009"
    E010_INTERNAL_ERROR = "E010"
    E011_INVALID_MODEL_SETTINGS = "E011"
    E012_MEDIA_UPLOAD_FAILED = "E012"


def format_user_error(code: str, message: str, lang: str = DEFAULT_LANGUAGE) -> str:
    return build_user_error_message(code, lang)


def build_insufficient_balance_message(lang: str = DEFAULT_LANGUAGE) -> str:
    return build_user_error_message(ErrorCode.E006_INSUFFICIENT_BALANCE, lang)


@dataclass(frozen=True)
class FlowTexts:
    initial_prompt: str
    second_step_prompt: str = ""
    missing_prompt: str = ""
    missing_media: str = ""
    invalid_media: str = ""
    invalid_specific_media: str = ""


@dataclass(frozen=True, init=False)
class OutputDeliveryResult:
    success: bool
    method: str
    error_code: Optional[str]
    error_message: Optional[str]
    use_r2: bool

    def __init__(
        self,
        success: Optional[bool] = None,
        method: str = "document",
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        use_r2: bool = False,
        delivered_successfully: Optional[bool] = None,
    ):
        resolved_success = delivered_successfully if success is None else success
        object.__setattr__(self, "success", bool(resolved_success))
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "error_message", error_message)
        object.__setattr__(self, "use_r2", use_r2)

    @property
    def delivered_successfully(self) -> bool:
        return self.success


SUMMARY_PROMPT_LIMIT = 1500
FREEFORM_SETTING_TYPES = {"text", "textarea", "number", "integer", "float", "audio", "media"}
NUMERIC_SETTING_TYPES = {"number", "integer", "float"}
NEGATIVE_PROMPT_SETTING_KEYS = {"exclude", "excluded_prompt", "negative", "negative_prompt", "avoid_prompt"}
NEGATIVE_PROMPT_LEGACY_TITLES = {"exclude", "excluded prompt", "negative", "negative prompt", "avoid prompt", "исключить"}


@dataclass(frozen=True)
class GenerationBatchSummary:
    """User-facing summary data for one generation request batch."""

    model: GenerationModel
    prompt: str
    settings: Mapping[str, Any]
    expected_count: int
    completed_count: int
    failed_count: int
    credits_spent: int


GENERATION_TYPE_LABELS = {
    "text_to_image": "🖼 Text → Image",
    "image_to_image": "🖼 Image → Image",
    "image_edit": "🧩 Image Edit",
    "text_to_video": "🎬 Text → Video",
    "image_to_video": "🎥 Image → Video",
    "reference_to_video": "🧭 Reference → Video",
    "video_edit": "🎞 Video Edit",
    "video_extend": "⏩ Video Extend",
    "lipsync": "🗣 Lipsync",
    "motion_control": "🎚 Motion Control",
    "avatar": "👥 Avatar",
    "audio_to_video": "🎙 Audio → Video",
    "video_to_audio": "🔊 Video → Audio",
    "effects": "✨ Effects",
    ALL_MODELS_CATEGORY: "📚 All models",
    LEGACY_ALL_MODELS_CATEGORY: "📚 All models",
}

PROVIDER_LABELS = {
    "alibaba": "Alibaba",
    "bytedance": "ByteDance",
    "google": "Google",
    "openai": "OpenAI",
    "kling": "Kling",
    "grok": "Grok",
    "minimax": "MiniMax",
    "wavespeed_ai": "Wan AI",
}


def get_generation_type_title(generation_type: str, lang: str) -> str:
    return t(f"generation.section_title.{generation_type}", lang)


def get_generation_type_description(generation_type: str, lang: str) -> str:
    return t(f"generation.section_details.{generation_type}", lang)


def is_all_models_category(generation_type: Any) -> bool:
    return generation_type in {ALL_MODELS_CATEGORY, LEGACY_ALL_MODELS_CATEGORY}


def extract_prediction_id(payload: Dict[str, Any]) -> Optional[str]:
    """Достать prediction_id из ответа Wavespeed."""
    return payload.get("prediction_id") or payload.get("id") or payload.get("data", {}).get("id")


def normalize_status(payload: Dict[str, Any]) -> str:
    """Нормализовать статус ответа Wavespeed."""
    status = (payload.get("status") or payload.get("state") or "").lower()
    if status in {"created", "queued", "starting"}:
        return "processing"
    if status in {"processing", "running", "in_progress"}:
        return "processing"
    if status in {"completed", "succeeded", "success"}:
        return "completed"
    if status in {"failed", "error", "cancelled", "canceled", "timeout"}:
        return "failed"
    return "processing"


def extract_output_urls(payload: Dict[str, Any]) -> list[str]:
    """Извлечь URL результатов из ответа Wavespeed."""
    value = payload.get("outputs") or payload.get("output_urls") or payload.get("urls") or payload.get("output") or []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, str)]
    return []


def format_generation_settings(model: GenerationModel, user_settings: dict[str, Any]) -> str:
    """Форматировать список текущих настроек модели."""
    if not model.user_settings:
        return "-"
    return "\n".join(
        f"- <b>{escape(get_setting_display_title(setting_key, setting, DEFAULT_LANGUAGE))}</b>: <code>{escape(str(user_settings.get(setting.key, setting.default)))}</code>"
        for setting_key, setting in model.user_settings.items()
    )


def get_setting_display_title(setting_key: str, setting: Any, lang: str = DEFAULT_LANGUAGE) -> str:
    if setting_key in NEGATIVE_PROMPT_SETTING_KEYS:
        return t("settings.title.negative_prompt", lang)
    direct_title_key = f"settings.{setting_key}"
    direct_translated_title = t(direct_title_key, lang)
    if direct_translated_title != direct_title_key:
        return direct_translated_title
    title_key = f"settings.title.{setting_key}"
    translated_title = t(title_key, lang)
    if translated_title != title_key:
        return translated_title
    fallback_title = str(getattr(setting, "title", setting_key))
    if fallback_title.strip().casefold() in NEGATIVE_PROMPT_LEGACY_TITLES:
        return t("settings.title.negative_prompt", lang)
    return fallback_title


def format_generation_settings_localized(model: GenerationModel, user_settings: dict[str, Any], lang: str = DEFAULT_LANGUAGE) -> str:
    if not model.user_settings:
        return "-"
    return "\n".join(
        f"- <b>{escape(get_setting_display_title(setting_key, setting, lang))}</b>: <code>{escape(str(user_settings.get(setting.key, setting.default)))}</code>"
        for setting_key, setting in model.user_settings.items()
    )


def get_generation_summary_setting_title(setting_key: str, setting: Any, lang: str = DEFAULT_LANGUAGE) -> str:
    if setting_key == "num_generations":
        return t("generation.summary.setting_generations", lang)
    return get_setting_display_title(setting_key, setting, lang)


def get_setting_display_value(setting: Any, value: Any) -> str:
    raw_value = str(value)
    for option in getattr(setting, "options", ()) or ():
        if option.value == raw_value:
            return option.label
    return raw_value


def setting_has_number_range(setting: Any) -> bool:
    return getattr(setting, "min_value", None) is not None and getattr(setting, "max_value", None) is not None


def format_numeric_bound(value: Any) -> str:
    decimal_value = Decimal(str(value))
    if decimal_value == decimal_value.to_integral_value():
        return str(int(decimal_value))
    return str(decimal_value.normalize())


def get_numeric_setting_example(setting: Any) -> str:
    if getattr(setting, "min_value", None) is not None:
        return format_numeric_bound(setting.min_value)
    return "5"


def build_number_setting_error(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs: Any) -> str:
    return t(key, lang, **kwargs)


def validate_numeric_setting_input(setting: Any, raw_text: str, lang: str = DEFAULT_LANGUAGE) -> tuple[str | None, str | None]:
    stripped_text = raw_text.strip()
    try:
        numeric_value = Decimal(stripped_text)
    except Exception:
        if setting_has_number_range(setting):
            return None, build_number_setting_error(
                "errors.number_required_range",
                lang,
                min=format_numeric_bound(setting.min_value),
                max=format_numeric_bound(setting.max_value),
                example=get_numeric_setting_example(setting),
            )
        return None, build_number_setting_error("errors.number_required", lang)

    if not numeric_value.is_finite():
        return None, build_number_setting_error("errors.number_required", lang)
    if getattr(setting, "type", "") == "integer" and numeric_value != numeric_value.to_integral_value():
        return None, build_number_setting_error("errors.integer_required", lang)
    if getattr(setting, "min_value", None) is not None and numeric_value < Decimal(str(setting.min_value)):
        return None, build_number_setting_error("errors.number_too_small", lang, min=format_numeric_bound(setting.min_value))
    if getattr(setting, "max_value", None) is not None and numeric_value > Decimal(str(setting.max_value)):
        return None, build_number_setting_error("errors.number_too_large", lang, max=format_numeric_bound(setting.max_value))

    if numeric_value == numeric_value.to_integral_value():
        return str(int(numeric_value)), None
    return str(numeric_value.normalize()), None


def truncate_generation_summary_prompt(prompt: str, limit: int = SUMMARY_PROMPT_LIMIT) -> str:
    if len(prompt) <= limit:
        return prompt
    return f"{prompt[: max(0, limit - 3)].rstrip()}..."


def format_generation_summary_settings(model: GenerationModel, user_settings: Mapping[str, Any], lang: str = DEFAULT_LANGUAGE) -> str:
    rows: list[str] = []
    for setting_key, setting in model.user_settings.items():
        if not getattr(setting, "is_user_visible", True):
            continue
        title = get_generation_summary_setting_title(setting_key, setting, lang)
        value = user_settings.get(setting.key, setting.default)
        display_value = get_setting_display_value(setting, value)
        rows.append(f"• {escape(title)}: <code>{escape(str(display_value))}</code>")
    return "\n".join(rows) if rows else "-"


def build_generation_summary_message(generation_batch: GenerationBatchSummary, lang: str = DEFAULT_LANGUAGE) -> str:
    """Build the final localized summary shown after all generation outputs in a batch."""
    prompt = str(generation_batch.prompt or "").strip() or t("generation.summary.no_prompt", lang)
    prompt = escape(truncate_generation_summary_prompt(prompt))
    generation_type = escape(get_generation_type_title(generation_batch.model.generation_type, lang))
    settings_text = format_generation_summary_settings(generation_batch.model, generation_batch.settings, lang)

    parts = [
        t("generation.summary.title", lang),
        "",
        t("generation.summary.model", lang, model=escape(generation_batch.model.title)),
        t("generation.summary.type", lang, generation_type=generation_type),
        t("generation.summary.prompt", lang, prompt=prompt),
        "",
        t("generation.summary.settings", lang, settings=settings_text),
        "",
        t(
            "generation.summary.results",
            lang,
            completed=generation_batch.completed_count,
            expected=generation_batch.expected_count,
        ),
    ]

    if generation_batch.failed_count:
        parts.extend(
            [
                t("generation.summary.partial_failed", lang, count=generation_batch.failed_count),
                t("generation.summary.refund_done", lang),
            ]
        )

    parts.extend(["", t("generation.summary.credits", lang, credits=generation_batch.credits_spent)])
    return "\n".join(parts)


def build_generation_batch_summary(generations: list[Any]) -> Optional[GenerationBatchSummary]:
    if not generations:
        return None
    first_generation = generations[0]
    try:
        model = get_generation_model(first_generation.model_key)
    except ValueError:
        logger.warning("Skipped generation summary for unsupported model: %s", first_generation.model_key)
        return None

    completed_count = sum(1 for generation in generations if generation.status == GenerationRequestStatus.COMPLETED)
    expected_count = len(generations)
    failed_count = max(expected_count - completed_count, 0)
    credits_spent = sum(int(generation.cost or 0) for generation in generations if generation.status == GenerationRequestStatus.COMPLETED)
    return GenerationBatchSummary(
        model=model,
        prompt=str(first_generation.prompt or ""),
        settings=dict(first_generation.settings or {}),
        expected_count=expected_count,
        completed_count=completed_count,
        failed_count=failed_count,
        credits_spent=credits_spent,
    )


def _requirement_is_required(model: GenerationModel, input_kind: str) -> bool:
    requirement = (model.input_requirements or {}).get(input_kind)
    if not isinstance(requirement, dict):
        return False
    if bool(requirement.get("required")):
        return True
    payload_field = str(requirement.get("payload_field") or "")
    return bool(payload_field and payload_field in set(model.required_payload_fields))


def describe_model_requirements(model: GenerationModel, lang: str = DEFAULT_LANGUAGE) -> str:
    """Describe required user inputs for the selected model."""
    requirement_keys: list[str] = []
    generation_type = model.generation_type

    if generation_type == "lipsync":
        requirement_keys.extend(["requirements.video_with_face", "requirements.audio"])
        lines = [f"<b>{escape(t('requirements.title', lang))}</b>", ""]
        lines.extend(f"• {escape(t(requirement_key, lang))}" for requirement_key in dict.fromkeys(requirement_keys))
        return "\n".join(lines)

    if _requirement_is_required(model, "video") or model.requires_video:
        requirement_keys.append("requirements.video")
    if _requirement_is_required(model, "images") or model.requires_image:
        if generation_type == "reference_to_video":
            requirement_keys.append("requirements.reference_images")
        elif model.input_media_field == "images" or model.supports_multiple_images:
            requirement_keys.append("requirements.images")
        else:
            requirement_keys.append("requirements.image")
    if _requirement_is_required(model, "audio") or model.requires_audio:
        requirement_keys.append("requirements.audio")
    if _requirement_is_required(model, "prompt") or model.requires_prompt:
        requirement_keys.append("requirements.continuation_prompt" if generation_type == "video_extend" else "requirements.prompt")
    elif isinstance((model.input_requirements or {}).get("prompt"), dict):
        requirement_keys.append("requirements.optional_prompt")

    if not requirement_keys:
        return ""
    lines = [f"<b>{escape(t('requirements.title', lang))}</b>", ""]
    lines.extend(f"• {escape(t(requirement_key, lang))}" for requirement_key in dict.fromkeys(requirement_keys))
    return "\n".join(lines)


def get_total_generation_cost(model: GenerationModel, user_settings: dict[str, Any]) -> int:
    return calculate_generation_cost_credits(
        model,
        user_settings,
        num_generations=get_model_num_generations(model, user_settings),
    )


def get_single_generation_cost(model: GenerationModel, user_settings: dict[str, Any]) -> int:
    return calculate_generation_cost_credits(model, user_settings, num_generations=1)


def format_price_usd(price_usd) -> str:
    return format(price_usd.quantize(Decimal("0.01")), "f")


def format_settings_price_line(model: GenerationModel, user_settings: dict[str, Any], lang: str = DEFAULT_LANGUAGE) -> str:
    num_generations = get_model_num_generations(model, user_settings)
    price_usd, cost = calculate_generation_price_quote(model, user_settings, num_generations=num_generations)
    usd_label = format_price_usd(price_usd)
    if lang == "ru":
        prefix = "от " if generation_cost_has_minimum_label(model) and not user_settings else ""
        if is_generation_cost_estimated(model):
            prefix = f"{prefix}≈ "
        return f"💰 Цена: {prefix}{cost} credits (~${usd_label})"
    prefix = "from " if generation_cost_has_minimum_label(model) and not user_settings else ""
    if is_generation_cost_estimated(model):
        prefix = f"{prefix}≈ "
    return f"💰 Price: {prefix}{cost} credits (~${usd_label})"


def build_settings_text(model: GenerationModel, user_settings: dict[str, Any], lang: str = DEFAULT_LANGUAGE) -> str:
    """Собрать экран настроек модели."""
    price_line = format_settings_price_line(model, user_settings, lang)
    requirements_text = describe_model_requirements(model, lang)
    requirements_block = f"{requirements_text}\n\n" if requirements_text else ""
    if not model.user_settings:
        return (
            f"{t('settings.model_settings', lang, model=escape(model.title))}\n\n"
            f"{requirements_block}"
            f"{price_line}\n\n"
            f"{t('generation.settings_no_extra_full', lang)}"
        )
    return (
        f"{t('settings.model_settings', lang, model=escape(model.title))}\n\n"
        f"{requirements_block}"
        f"{price_line}\n\n"
        f"{t('settings.choose_parameters', lang)}\n\n"
        f"{t('settings.current_values', lang, values=format_generation_settings_localized(model, user_settings, lang))}"
    )


def build_setting_value_text(model: GenerationModel, setting_key: str, current_value: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """Собрать экран выбора конкретной настройки."""
    setting = model.user_settings[setting_key]
    setting_title = get_setting_display_title(setting_key, setting, lang)
    if setting.type in FREEFORM_SETTING_TYPES:
        if setting.type in NUMERIC_SETTING_TYPES:
            prompt_text = (
                t(
                    "settings.enter_number_value_range",
                    lang,
                    min=format_numeric_bound(setting.min_value),
                    max=format_numeric_bound(setting.max_value),
                )
                if setting_has_number_range(setting)
                else t("settings.enter_number_value", lang)
            )
        else:
            prompt_text = t("settings.enter_text_value", lang)
        return (
            f"{t('settings.parameter', lang, parameter=escape(setting_title))}\n"
            f"{t('settings.current_value', lang, value=escape(current_value))}\n\n"
            f"{prompt_text}\n"
            f"{t('settings.clear_hint', lang)}"
        )
    return (
        f"{t('settings.parameter', lang, parameter=escape(setting_title))}\n"
        f"{t('settings.current_value', lang, value=escape(current_value))}\n\n"
        f"{t('settings.select_helper', lang)}"
    )


def build_confirmation_text(
    model: GenerationModel,
    user_settings: dict[str, Any],
    prompt: str,
    balance: int,
    lang: str = DEFAULT_LANGUAGE,
) -> str:
    """Собрать экран подтверждения генерации."""
    num_generations = get_model_num_generations(model, user_settings)
    total_cost = get_total_generation_cost(model, user_settings)
    balance_after_launch = max(balance - total_cost, 0)
    prompt_line = t("generation.prompt_label", lang, prompt=escape(prompt))
    if model.generation_type == "lipsync":
        prompt_line = t("generation.voiceover_label", lang, prompt=escape(prompt))
    return (
        f"{t('generation.review', lang)}\n\n"
        f"{t('generation.model_label', lang, model=escape(model.title))}\n"
        f"{t('generation.settings_label', lang, settings=format_generation_settings_localized(model, user_settings, lang))}\n\n"
        f"{prompt_line}\n\n"
        f"{t('generation.count_label', lang, count=num_generations)}\n"
        f"{t('generation.cost_label', lang, cost=total_cost)}\n"
        f"{t('generation.balance_after_label', lang, balance=balance_after_launch)}"
    )


def build_partial_generation_failed_message(lang: str = DEFAULT_LANGUAGE) -> str:
    return build_user_error_message(ErrorCode.E007_WAVESPEED_FAILED, lang)


def build_generated_but_delivery_failed_message(lang: str = DEFAULT_LANGUAGE) -> str:
    return build_user_error_message(ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, lang)


def build_telegram_delivery_failed_refund_message(lang: str = DEFAULT_LANGUAGE) -> str:
    return build_user_error_message(ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, lang)


def build_empty_outputs_failed_message(lang: str = DEFAULT_LANGUAGE) -> str:
    return build_user_error_message(ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, lang)


def get_user_friendly_error_message(error: Exception, result: Optional[WavespeedResult] = None, lang: str = DEFAULT_LANGUAGE) -> str:
    """Вернуть безопасное и понятное сообщение об ошибке для пользователя."""
    if isinstance(error, WavespeedTimeoutError):
        return build_user_error_message(ErrorCode.E008_WAVESPEED_TIMEOUT, lang)

    if isinstance(error, WavespeedFailedError) or (result is not None and result.status == "failed"):
        return build_user_error_message(ErrorCode.E007_WAVESPEED_FAILED, lang)

    if isinstance(error, TelegramBadRequest):
        return build_user_error_message(ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, lang)

    if isinstance(error, (WavespeedNetworkError, aiohttp.ClientError, TimeoutError)):
        return build_user_error_message(ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, lang)

    return build_user_error_message(ErrorCode.E010_INTERNAL_ERROR, lang)


class OutputDeliveryTooLargeError(Exception):
    """Файл результата слишком большой для отправки в Telegram."""


def get_model_state_settings(state_data: dict[str, Any], model_key: str) -> dict[str, Any]:
    """Получить провалидированные настройки модели из FSM."""
    return validate_model_settings(model_key, state_data.get("user_settings"))


def is_lipsync_generation_state(state_data: dict[str, Any]) -> bool:
    """Определить, что текущий сценарий относится к lipsync."""
    return state_data.get("model_generation_type") == "lipsync"


def get_input_audio_or_text_display(value: Any, lang: str = DEFAULT_LANGUAGE) -> str:
    """Вернуть пользовательское описание текстового или аудио-входа."""
    if not isinstance(value, dict):
        return ""
    source_type = value.get("type")
    if source_type == "text":
        return str(value.get("text") or "")
    if source_type == "voice":
        return t("generation.voice_message", lang)
    if source_type == "audio":
        return t("generation.audio_file", lang)
    return ""


def get_media_input_prompt_text(*, is_lipsync: bool, lang: str = DEFAULT_LANGUAGE) -> str:
    """Вернуть текст шага загрузки media-входа."""
    if is_lipsync:
        return get_flow_texts("lipsync", lang).initial_prompt
    return get_flow_texts("image_edit", lang).initial_prompt


def get_lipsync_incomplete_error_text(lang: str = DEFAULT_LANGUAGE) -> str:
    """Вернуть единое сообщение о неполных входных данных lipsync."""
    return build_user_error_message("generation.lipsync_incomplete", lang)


def is_audio_input_payload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") in {"voice", "audio"}


def is_text_input_payload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "text" and bool(str(value.get("text") or "").strip())


def state_has_required_media(model: GenerationModel, input_media: Any, input_media_items: list[dict[str, str]]) -> bool:
    if model.input_media_field == "images":
        has_legacy_single_image = bool(isinstance(input_media, dict) and input_media.get("type") in {"photo", "image", "images"})
        return len(input_media_items) >= model.min_images or has_legacy_single_image
    if model.input_media_field == "image":
        return bool(isinstance(input_media, dict) and input_media.get("type") in {"photo", "image"})
    if model.input_media_field == "video":
        return bool(isinstance(input_media, dict) and input_media.get("type") == "video")
    return True


def state_has_required_prompt_or_audio(model: GenerationModel, prompt: str, input_audio_or_text: Any) -> bool:
    prompt_required = model_requires_prompt_input(model)
    audio_required = model_requires_audio_file(model)
    if prompt_required and audio_required:
        return bool(prompt.strip()) and is_audio_input_payload(input_audio_or_text)
    if prompt_required:
        if model.generation_type == "lipsync":
            return is_text_input_payload(input_audio_or_text)
        return bool(prompt.strip())
    if audio_required:
        return is_audio_input_payload(input_audio_or_text)
    return True


def get_second_step_prompt_text(*, is_lipsync: bool, lang: str = DEFAULT_LANGUAGE) -> str:
    """Вернуть текст второго шага после загрузки media."""
    if is_lipsync:
        return get_flow_texts("lipsync", lang).second_step_prompt
    return t("generation.second_step_text", lang)


def model_requires_audio_file(model: GenerationModel) -> bool:
    audio_requirement = (model.input_requirements or {}).get("audio")
    if isinstance(audio_requirement, dict):
        return bool(audio_requirement.get("required"))
    return bool(model.requires_audio)


def model_requires_prompt_input(model: GenerationModel) -> bool:
    prompt_requirement = (model.input_requirements or {}).get("prompt")
    if isinstance(prompt_requirement, dict):
        return bool(prompt_requirement.get("required"))
    if isinstance(prompt_requirement, bool):
        return prompt_requirement
    return bool(model.requires_prompt)


def get_audio_max_size_bytes(model: GenerationModel) -> int | None:
    audio_requirement = (model.input_requirements or {}).get("audio")
    if not isinstance(audio_requirement, dict):
        return None
    max_size_mb = audio_requirement.get("max_size_mb")
    if max_size_mb is None:
        return AUDIO_MAX_SIZE_MB * 1024 * 1024
    try:
        return int(max_size_mb) * 1024 * 1024
    except (TypeError, ValueError):
        return None


def get_flow_texts(generation_type: str, lang: str = DEFAULT_LANGUAGE) -> FlowTexts:
    base = FlowTexts(
        initial_prompt=t("generation.flow.text_to_image.initial", lang),
        missing_prompt=t("errors.e002", lang).lower() + ".",
    )
    flows = {
        "text_to_image": FlowTexts(
            initial_prompt=t("generation.flow.text_to_image.initial", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
        ),
        "text_to_video": FlowTexts(
            initial_prompt=t("generation.flow.text_to_video.initial", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
        ),
        "image_edit": FlowTexts(
            initial_prompt=t("generation.flow.image_edit.initial", lang),
            second_step_prompt=t("generation.second_step_text", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
            missing_media=t("generation.flow.image.missing", lang),
            invalid_media=t("generation.flow.image.invalid", lang),
            invalid_specific_media=t("generation.flow.image.invalid_specific", lang),
        ),
        "image_to_video": FlowTexts(
            initial_prompt=t("generation.flow.image_to_video.initial", lang),
            second_step_prompt=t("generation.second_step_text", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
            missing_media=t("generation.flow.image.missing", lang),
            invalid_media=t("generation.flow.image.invalid", lang),
            invalid_specific_media=t("generation.flow.image.invalid_specific", lang),
        ),
        "video_edit": FlowTexts(
            initial_prompt=t("generation.flow.video_edit.initial", lang),
            second_step_prompt=t("generation.second_step_text", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
            missing_media=t("generation.flow.video.missing", lang),
            invalid_media=t("generation.flow.video.invalid", lang),
            invalid_specific_media=t("generation.flow.video.invalid_specific", lang),
        ),
        "video_extend": FlowTexts(
            initial_prompt=t("generation.flow.video_extend.initial", lang),
            second_step_prompt=t("generation.second_step_text", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
            missing_media=t("generation.flow.video.missing", lang),
            invalid_media=t("generation.flow.video.invalid", lang),
            invalid_specific_media=t("generation.flow.video.invalid_specific", lang),
        ),
        "reference_to_video": FlowTexts(
            initial_prompt=t("generation.flow.reference_to_video.initial", lang),
            second_step_prompt=t("generation.second_step_text", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
            missing_media=t("generation.flow.image.missing", lang),
            invalid_media=t("generation.flow.image.invalid", lang),
            invalid_specific_media=t("generation.flow.image.invalid_specific", lang),
        ),
        "lipsync": FlowTexts(
            initial_prompt=t("generation.flow.lipsync.initial", lang),
            second_step_prompt=t("generation.flow.lipsync.second", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
        ),
    }
    return flows.get(generation_type, base)


def get_prompt_for_generation_type(generation_type: str, lang: str = DEFAULT_LANGUAGE) -> str:
    return get_flow_texts(generation_type, lang).initial_prompt


def get_second_prompt_for_generation_type(generation_type: str, lang: str = DEFAULT_LANGUAGE) -> str:
    return get_flow_texts(generation_type, lang).second_step_prompt or t("generation.prompt_request", lang)


def extract_document_media_type(document: Any) -> str:
    mime_type = (getattr(document, "mime_type", "") or "").lower()
    filename = (getattr(document, "file_name", "") or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    if any(filename.endswith(extension) for extension in AUDIO_FILE_EXTENSIONS):
        return "audio"
    return "unknown"


def message_contains_file(message: Message) -> bool:
    return bool(message.photo or message.video or message.document or message.voice or message.audio)


def is_supported_image_input(message: Message) -> bool:
    if message.photo:
        return True
    if message.document:
        return extract_document_media_type(message.document) == "image"
    return False


def is_supported_video_input(message: Message) -> bool:
    if message.video:
        return True
    if message.document:
        return extract_document_media_type(message.document) == "video"
    return False


def is_supported_audio_input(message: Message) -> bool:
    if message.voice or message.audio:
        return True
    if message.document:
        return extract_document_media_type(message.document) == "audio"
    return False


def get_audio_input_file(message: Message) -> Any:
    return message.audio or message.voice or message.document


def get_audio_input_file_size(message: Message) -> int | None:
    audio_file = get_audio_input_file(message)
    file_size = getattr(audio_file, "file_size", None)
    return int(file_size) if isinstance(file_size, int) else None


def get_waiting_state_for_input_type(required_input_type: str):
    if required_input_type == "image":
        return GenerationStates.waiting_for_image
    if required_input_type == "video":
        return GenerationStates.waiting_for_video
    if required_input_type == "audio":
        return GenerationStates.waiting_for_audio
    return GenerationStates.waiting_for_prompt


def build_invalid_input_message(required_input_type: str, generation_type: str, *, received_type: Optional[str] = None, lang: str = DEFAULT_LANGUAGE) -> str:
    if required_input_type == "text":
        return build_user_error_message("errors.prompt_text_only", lang)
    if required_input_type == "image":
        return build_user_error_message(ErrorCode.E003_MISSING_IMAGE, lang)
    if required_input_type == "video":
        return build_user_error_message(ErrorCode.E004_MISSING_VIDEO, lang)
    if required_input_type == "audio":
        return build_user_error_message("errors.waiting_audio", lang)
    return build_user_error_message(ErrorCode.E001_INVALID_INPUT_TYPE, lang)


def log_generation_error(
    error_code: str,
    *,
    generation_id: Any = None,
    user_id: Optional[int] = None,
    model_key: Optional[str] = None,
    status: str = "failed",
    details: Optional[str] = None,
) -> None:
    log_error_code(
        error_code,
        {
            "action": "generation_error",
            "generation_id": generation_id,
            "user_id": user_id,
            "model_key": model_key,
            "status": status,
            "details": details,
        },
    )


def get_incoming_text_type(message: Optional[Message] = None, *, is_callback: bool = False) -> str:
    if is_callback:
        return "callback"
    if message is None:
        return "unknown"
    if message.text is not None:
        return "text"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.document:
        return "document"
    return "unknown"


def build_generation_diagnostic_payload(
    *,
    action: str,
    user_id: Optional[int],
    state_value: Any,
    state_data: dict[str, Any],
    incoming_text_type: str,
    prompt: Optional[str] = None,
    total_cost: Optional[int] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    model_key = state_data.get("selected_model_key") or state_data.get("model_key")
    selected_generation_type = state_data.get("selected_generation_type") or state_data.get("model_generation_type")
    user_settings: dict[str, Any] = {}
    if isinstance(state_data.get("selected_settings"), dict):
        user_settings = dict(state_data.get("selected_settings") or {})
    elif model_key:
        user_settings = get_model_state_settings(state_data, str(model_key))
    elif isinstance(state_data.get("user_settings"), dict):
        user_settings = dict(state_data.get("user_settings") or {})

    model = None
    if model_key:
        try:
            model = get_generation_model(str(model_key))
        except ValueError:
            model = None

    num_generations: Optional[int] = None
    if model is not None:
        num_generations = get_model_num_generations(model, user_settings)
        if total_cost is None:
            total_cost = get_total_generation_cost(model, user_settings)
    elif "num_generations" in user_settings:
        try:
            num_generations = int(str(user_settings.get("num_generations") or "0"))
        except (TypeError, ValueError):
            num_generations = None

    prompt_value = prompt if prompt is not None else state_data.get("prompt")
    payload = {
        "action": action,
        "user_id": user_id,
        "state": normalize_generation_state(state_value),
        "current_state": normalize_generation_state(state_value),
        "incoming_text_type": incoming_text_type,
        "model_key": model_key,
        "selected_model_key": model_key,
        "selected_generation_type": selected_generation_type,
        "num_generations": num_generations,
        "total_cost": total_cost,
        "prompt_length": len(prompt_value) if prompt_value else 0,
    }
    if extra:
        payload.update(extra)
    return payload


def log_generation_diagnostic(
    *,
    action: str,
    user_id: Optional[int],
    state_value: Any,
    state_data: dict[str, Any],
    incoming_text_type: str,
    prompt: Optional[str] = None,
    total_cost: Optional[int] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    logger.info(
        build_generation_diagnostic_payload(
            action=action,
            user_id=user_id,
            state_value=state_value,
            state_data=state_data,
            incoming_text_type=incoming_text_type,
            prompt=prompt,
            total_cost=total_cost,
            extra=extra,
        )
    )


def build_input_media_payload(message: Message) -> dict[str, str]:
    """Собрать метаданные входного media-файла из Telegram message."""
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id}
    if message.document:
        mime_type = (message.document.mime_type or "").lower()
        media_type = "video" if mime_type.startswith("video/") else "image"
        return {"type": media_type, "file_id": message.document.file_id}
    return {}


def get_input_media_items(state_data: dict[str, Any]) -> list[dict[str, str]]:
    urls = get_input_media_urls(state_data)
    if urls:
        paths = get_input_media_paths(state_data)
        file_ids = get_input_media_file_ids(state_data)
        return [
            {
                "type": "image",
                "file_id": file_ids[index] if index < len(file_ids) else "",
                "local_path": paths[index] if index < len(paths) else "",
                "public_url": public_url,
            }
            for index, public_url in enumerate(urls)
        ]

    raw_items = state_data.get("input_media_items")
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _get_string_list(state_data: dict[str, Any], key: str) -> list[str]:
    raw_values = state_data.get(key)
    if not isinstance(raw_values, list):
        return []
    return [value for value in raw_values if isinstance(value, str) and value]


def get_input_media_urls(state_data: dict[str, Any]) -> list[str]:
    urls = _get_string_list(state_data, "input_media_urls")
    if urls:
        return urls
    raw_items = state_data.get("input_media_items")
    if not isinstance(raw_items, list):
        return []
    return [str(item.get("public_url")) for item in raw_items if isinstance(item, dict) and item.get("public_url")]


def get_input_media_paths(state_data: dict[str, Any]) -> list[str]:
    paths = _get_string_list(state_data, "input_media_paths")
    if paths:
        return paths
    raw_items = state_data.get("input_media_items")
    if not isinstance(raw_items, list):
        return []
    return [str(item.get("local_path")) for item in raw_items if isinstance(item, dict) and item.get("local_path")]


def get_input_media_file_ids(state_data: dict[str, Any]) -> list[str]:
    file_ids = _get_string_list(state_data, "input_media_file_ids")
    if file_ids:
        return file_ids
    raw_items = state_data.get("input_media_items")
    if not isinstance(raw_items, list):
        return []
    return [str(item.get("file_id")) for item in raw_items if isinstance(item, dict) and item.get("file_id")]


def build_input_media_items_from_lists(
    urls: list[str],
    paths: list[str],
    file_ids: list[str],
    media_type: str = "image",
) -> list[dict[str, str]]:
    return [
        {
            "type": media_type,
            "file_id": file_ids[index] if index < len(file_ids) else "",
            "local_path": paths[index] if index < len(paths) else "",
            "public_url": public_url,
        }
        for index, public_url in enumerate(urls)
    ]


def get_media_group_key(message: Message) -> str | None:
    media_group_id = getattr(message, "media_group_id", None)
    if not media_group_id:
        return None
    return f"{message.from_user.id}:{message.chat.id}:{media_group_id}"


def append_media_items_to_state_data(
    state_data: dict[str, Any],
    media_items: list[dict[str, str]],
    *,
    max_images: int,
) -> tuple[dict[str, Any], int, bool]:
    current_urls = get_input_media_urls(state_data)
    current_paths = get_input_media_paths(state_data)
    current_file_ids = get_input_media_file_ids(state_data)
    seen_file_ids = {file_id for file_id in current_file_ids if file_id}
    seen_urls = {url for url in current_urls if url}
    added_count = 0
    limit_reached = len(current_urls) >= max_images

    for media_item in media_items:
        if len(current_urls) >= max_images:
            limit_reached = True
            break
        public_url = media_item.get("public_url")
        local_path = media_item.get("local_path")
        file_id = media_item.get("file_id", "")
        if not public_url or not local_path:
            continue
        if (file_id and file_id in seen_file_ids) or public_url in seen_urls:
            continue
        current_urls.append(public_url)
        current_paths.append(local_path)
        current_file_ids.append(file_id)
        if file_id:
            seen_file_ids.add(file_id)
        seen_urls.add(public_url)
        added_count += 1

    updated_items = build_input_media_items_from_lists(current_urls, current_paths, current_file_ids)
    return (
        {
            "input_media_items": updated_items,
            "input_media_urls": current_urls,
            "input_media_paths": current_paths,
            "input_media_file_ids": current_file_ids,
            "input_media": {"type": "images", "count": len(current_urls)} if current_urls else None,
            "input_image_file_id": current_file_ids[0] if current_file_ids else None,
        },
        added_count,
        limit_reached,
    )


async def cleanup_media_items(media_items: list[dict[str, str]]) -> None:
    cleaned_paths: set[str] = set()
    for item in media_items:
        local_path = item.get("local_path")
        if local_path and local_path not in cleaned_paths:
            Path(local_path).unlink(missing_ok=True)
            cleaned_paths.add(local_path)


async def cleanup_state_media(state: FSMContext) -> None:
    state_data = await state.get_data()
    media_items = get_input_media_items(state_data)
    await cleanup_media_items(media_items)
    input_audio_path = state_data.get("input_audio_path")
    if isinstance(input_audio_path, str) and input_audio_path:
        Path(input_audio_path).unlink(missing_ok=True)
    await state.update_data(
        input_media=None,
        input_media_items=[],
        input_media_urls=[],
        input_media_paths=[],
        input_media_file_ids=[],
        input_image_file_id=None,
        input_audio_url=None,
        input_audio_path=None,
        input_audio_file_id=None,
        input_audio_or_text=None,
    )


async def set_waiting_for_prompt_with_diagnostic(
    state: FSMContext,
    *,
    user_id: int,
    incoming_text_type: str,
) -> None:
    await state.set_state(GenerationStates.waiting_for_prompt)
    state_data = await state.get_data()
    log_generation_diagnostic(
        action="enter_waiting_for_prompt",
        user_id=user_id,
        state_value=GenerationStates.waiting_for_prompt.state,
        state_data=state_data,
        incoming_text_type=incoming_text_type,
    )


async def upload_message_media_item(message: Message) -> dict[str, str]:
    input_media = build_input_media_payload(message)
    telegram_files = TelegramFilesService(message.bot)
    temp_media = await telegram_files.download_temp_file_and_get_public_url(str(input_media.get("file_id")))
    return {
        "type": str(input_media.get("type") or "image"),
        "file_id": str(input_media.get("file_id") or ""),
        "local_path": str(temp_media.local_path),
        "public_url": temp_media.public_url,
    }


async def add_media_group_message(message: Message, state: FSMContext, *, mode: str) -> None:
    media_group_id = getattr(message, "media_group_id", None)
    group_key = get_media_group_key(message)
    if not media_group_id or not group_key:
        return

    MEDIA_GROUP_BUFFERS.setdefault(group_key, []).append(message)
    MEDIA_GROUP_STATES[group_key] = state
    MEDIA_GROUP_MODES[group_key] = mode
    existing_task = MEDIA_GROUP_TASKS.get(group_key)
    if existing_task and not existing_task.done():
        existing_task.cancel()
    MEDIA_GROUP_TASKS[group_key] = asyncio.create_task(process_media_group_after_delay(group_key))
    log_media_group_event(
        "media_group_received",
        message,
        media_group_id=str(media_group_id),
        buffered_count=len(MEDIA_GROUP_BUFFERS[group_key]),
        mode=mode,
    )


async def process_media_group_after_delay(group_key: str) -> None:
    try:
        await asyncio.sleep(MEDIA_GROUP_DEBOUNCE_SECONDS)
        messages = MEDIA_GROUP_BUFFERS.pop(group_key, [])
        state = MEDIA_GROUP_STATES.pop(group_key, None)
        mode = MEDIA_GROUP_MODES.pop(group_key, "multi_image")
        MEDIA_GROUP_TASKS.pop(group_key, None)
        if not messages or state is None:
            return
        if mode == "single_image":
            await process_single_image_media_group(messages, state)
            return
        await process_multi_image_media_group(messages, state)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Media group processing failed")


async def process_single_image_media_group(messages: list[Message], state: FSMContext) -> None:
    first_message = messages[0]
    media_group_id = str(getattr(first_message, "media_group_id", ""))
    state_data = await state.get_data()
    lang = get_state_language(state_data, first_message.from_user)
    image_messages = [message for message in messages if is_supported_image_input(message)]
    skipped_count = len(messages) - len(image_messages)
    if not image_messages:
        log_media_group_event(
            "media_group_processed",
            first_message,
            media_group_id=media_group_id,
            added_count=0,
            skipped_count=skipped_count,
            mode="single_image",
        )
        await first_message.answer(
            build_invalid_input_message("image", "image_edit", lang=lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    await process_generation_image(image_messages[0], state, from_media_group=True)
    ignored_count = max(0, len(image_messages) - 1) + skipped_count
    log_media_group_event(
        "media_group_processed",
        first_message,
        media_group_id=media_group_id,
        added_count=1,
        skipped_count=ignored_count,
        mode="single_image",
    )
    if ignored_count:
        await first_message.answer(t("generation.single_image_album_first_used", lang))


async def process_multi_image_media_group(messages: list[Message], state: FSMContext) -> None:
    first_message = messages[0]
    media_group_id = str(getattr(first_message, "media_group_id", ""))
    state_data = await state.get_data()
    lang = get_state_language(state_data, first_message.from_user)
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    if not model:
        await first_message.answer(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang))
        return

    current_urls = get_input_media_urls(state_data)
    capacity = max(0, model.max_images - len(current_urls))
    image_messages = [message for message in messages if is_supported_image_input(message)]
    skipped_non_image_count = len(messages) - len(image_messages)
    selected_messages = image_messages[:capacity]
    limit_reached = len(image_messages) > len(selected_messages) or capacity <= 0
    uploaded_items: list[dict[str, str]] = []
    seen_file_ids = set(get_input_media_file_ids(state_data))
    for album_message in selected_messages:
        input_media = build_input_media_payload(album_message)
        file_id = str(input_media.get("file_id") or "")
        if file_id and file_id in seen_file_ids:
            continue
        if file_id:
            seen_file_ids.add(file_id)
        try:
            uploaded_items.append(await upload_message_media_item(album_message))
        except ImageUploadError:
            log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=album_message.from_user.id, status="failed")

    state_data = await state.get_data()
    update_payload, added_count, append_limit_reached = append_media_items_to_state_data(
        state_data,
        uploaded_items,
        max_images=model.max_images,
    )
    await state.update_data(**update_payload)
    updated_count = len(update_payload["input_media_urls"])
    limit_reached = limit_reached or append_limit_reached or updated_count >= model.max_images
    if added_count:
        log_media_group_event(
            "media_group_images_added",
            first_message,
            media_group_id=media_group_id,
            added_count=added_count,
            total_count=updated_count,
            mode="multi_image",
        )
    if limit_reached:
        log_media_group_event(
            "media_group_limit_reached",
            first_message,
            media_group_id=media_group_id,
            total_count=updated_count,
            max_images=model.max_images,
            mode="multi_image",
        )
    log_media_group_event(
        "media_group_processed",
        first_message,
        media_group_id=media_group_id,
        added_count=added_count,
        skipped_count=skipped_non_image_count + max(0, len(image_messages) - len(selected_messages)),
        total_count=updated_count,
        mode="multi_image",
    )

    if limit_reached:
        await first_message.answer(
            t("generation.image_limit_reached", lang, count=model.max_images),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang, show_clear_images=bool(updated_count)),
        )
        return
    await first_message.answer(
        t("generation.images_uploaded_progress", lang, count=updated_count, max_count=model.max_images),
        reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang, show_clear_images=bool(updated_count)),
    )


def build_input_audio_or_text_payload(message: Message) -> dict[str, str]:
    """Собрать текстовый или аудио-вход для озвучки."""
    text = (message.text or "").strip()
    if text:
        return {"type": "text", "text": text}
    if message.voice:
        return {"type": "voice", "file_id": message.voice.file_id}
    if message.audio:
        return {"type": "audio", "file_id": message.audio.file_id}
    if message.document and extract_document_media_type(message.document) == "audio":
        return {"type": "audio", "file_id": message.document.file_id}
    return {}


def is_supported_media_document(document: Any, *, is_lipsync: bool) -> bool:
    """Проверить, что document подходит для текущего сценария генерации."""
    mime_type = (getattr(document, "mime_type", "") or "").lower()
    if is_lipsync:
        return mime_type.startswith("image/") or mime_type.startswith("video/")
    return mime_type.startswith("image/")


async def prompt_for_generation_input(message: Message, *, edit: bool, is_lipsync: bool, lang: str | None = None) -> None:
    """Показать шаг загрузки media-входа с reply keyboard возврата к настройкам."""
    lang = lang or get_actor_language(getattr(message, "from_user", None))
    prompt_text = get_media_input_prompt_text(is_lipsync=is_lipsync, lang=lang)
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        t("generation.changed_mind_back_to_settings", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


def build_generation_types_screen_text(lang: str = DEFAULT_LANGUAGE) -> str:
    """Собрать текст экрана выбора типа генерации."""
    details = "\n".join(
        f"• <b>{escape(get_generation_type_title(generation_type, lang))}</b> — {escape(get_generation_type_description(generation_type, lang))}"
        for generation_type in list_generation_types()
    )
    return f"{t('generation.choose_type', lang)}:\n\n{details}"


def build_generation_type_options() -> list[tuple[str, str]]:
    """Собрать опции клавиатуры выбора типа генерации."""
    available_generation_types = set(list_generation_types())
    ordered_generation_types = [
        generation_type
        for generation_type in GENERATION_CATEGORIES
        if generation_type != ALL_MODELS_CATEGORY and generation_type in available_generation_types
    ]
    options = [
        (generation_type, GENERATION_TYPE_LABELS[generation_type])
        for generation_type in ordered_generation_types
    ]
    options.append((ALL_MODELS_CATEGORY, GENERATION_TYPE_LABELS[ALL_MODELS_CATEGORY]))
    return options


def log_generation_callback(callback: CallbackQuery) -> None:
    """Логировать callback из раздела генерации."""
    logger.info(
        {
            "action": "generation_callback",
            "user_id": callback.from_user.id,
            "callback_data": callback.data,
        }
    )


def get_selected_models_for_state(state_data: dict[str, Any]) -> list[GenerationModel]:
    """Получить активный список моделей для текущего экрана выбора."""
    selected_provider = state_data.get("selected_provider")
    selected_generation_type = state_data.get("selected_generation_type")

    if selected_provider:
        return list_models_by_provider(str(selected_provider))
    if selected_generation_type and not is_all_models_category(selected_generation_type):
        return list_models_by_type(str(selected_generation_type))
    return []


async def render_models_screen(message: Message, lang: str | None = None) -> None:
    """Показать список типов генерации."""
    lang = lang or get_actor_language(message.from_user)
    await message.answer(
        build_generation_types_screen_text(lang),
        reply_markup=build_generation_sections_keyboard(lang),
        parse_mode="HTML",
    )


async def render_provider_screen(message: Message, *, edit: bool, page: int = 0, lang: str | None = None) -> None:
    """Показать список провайдеров для выбора моделей."""
    lang = lang or get_actor_language(getattr(message, "from_user", None))
    text = t("generation.choose_provider", lang)
    if edit:
        await message.edit_text(
            text,
            reply_markup=build_providers_keyboard(lang, page),
            parse_mode="HTML",
        )
        return
    await message.answer(
        text,
        reply_markup=build_providers_keyboard(lang, page),
        parse_mode="HTML",
    )


async def render_model_list_screen(
    message: Message,
    *,
    models: list[GenerationModel],
    edit: bool,
    heading: str,
    back_callback: str,
    page: int = 0,
    page_callback_builder: Any | None = None,
    lang: str | None = None,
) -> None:
    """Показать список моделей для выбранного типа или провайдера."""
    lang = lang or get_actor_language(getattr(message, "from_user", None))
    if edit:
        await message.edit_text(
            heading,
            reply_markup=build_models_keyboard(models, back_callback, lang, page, page_callback_builder),
            parse_mode="HTML",
        )
        return
    await message.answer(
        heading,
        reply_markup=build_models_keyboard(models, back_callback, lang, page, page_callback_builder),
        parse_mode="HTML",
    )


async def render_settings_screen(message: Message, state: FSMContext) -> None:
    """Показать экран настроек выбранной модели."""
    await render_settings_screen_message(message, state, edit=True)


async def render_settings_screen_message(message: Message, state: FSMContext, *, edit: bool) -> None:
    """Показать экран настроек выбранной модели через edit или обычное сообщение."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    lang = get_state_language(state_data, getattr(message, "from_user", None))
    if not model_key:
        if edit:
            await message.edit_text(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang), reply_markup=None)
        else:
            await message.answer(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang), reply_markup=get_main_menu_keyboard(lang))
        return

    model = get_generation_model(model_key)
    user_settings = get_model_state_settings(state_data, model_key)
    if edit:
        await message.edit_text(
            build_settings_text(model, user_settings, lang),
            reply_markup=build_model_settings_keyboard(model, user_settings, lang),
            parse_mode="HTML",
        )
        return

    await message.answer(
        build_settings_text(model, user_settings, lang),
        reply_markup=build_model_settings_keyboard(model, user_settings, lang),
        parse_mode="HTML",
    )


async def prompt_for_generation_image(message: Message, *, edit: bool, model: GenerationModel, lang: str | None = None) -> None:
    """Показать шаг загрузки изображения с reply keyboard возврата к настройкам."""
    lang = lang or get_actor_language(getattr(message, "from_user", None))
    prompt_text = t("generation.send_image_for_model", lang, model=model.title)
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        t("generation.changed_mind_back_to_settings", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


async def prompt_for_generation_images(message: Message, *, edit: bool, model: GenerationModel, lang: str | None = None) -> None:
    lang = lang or get_actor_language(getattr(message, "from_user", None))
    prompt_text = t(
        "generation.send_images_for_model",
        lang,
        model=model.title,
        min_count=model.min_images,
        max_count=model.max_images,
    )
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        t("generation.changed_mind_back_to_settings", lang),
        reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
    )


async def prompt_for_generation_video(message: Message, *, edit: bool, model: GenerationModel, lang: str | None = None) -> None:
    """Показать шаг загрузки видео с reply keyboard возврата к настройкам."""
    lang = lang or get_actor_language(getattr(message, "from_user", None))
    prompt_text = t("generation.send_video_for_lipsync", lang) if model.generation_type == "lipsync" else t("generation.send_video_for_model", lang, model=model.title)
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        t("generation.changed_mind_back_to_settings", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


async def prompt_for_generation_audio(message: Message, *, edit: bool, model: GenerationModel, lang: str | None = None) -> None:
    """Показать шаг загрузки аудио с reply keyboard возврата к настройкам."""
    lang = lang or get_actor_language(getattr(message, "from_user", None))
    prompt_text = t("generation.send_audio_for_lipsync", lang) if model.generation_type == "lipsync" else t("generation.send_audio_for_model", lang, model=model.title)
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        t("generation.send_audio_description", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


async def show_confirmation_if_media_completes_model(message: Message, state: FSMContext, model: GenerationModel) -> bool:
    if model_requires_prompt_input(model) or model_requires_audio_file(model):
        return False
    async with db_manager.session_factory() as session:
        await send_confirmation_screen(
            message=message,
            state=state,
            session=session,
            telegram_user=message.from_user,
            edit=False,
        )
    return True


async def prompt_for_repeat_media(message: Message, state: FSMContext, model: GenerationModel, *, edit: bool, lang: str | None = None) -> None:
    state_data = await state.get_data()
    lang = lang or get_state_language(state_data, getattr(message, "from_user", None))
    required_input_type = get_model_required_input_type(model)
    if required_input_type == "image":
        if model.supports_multiple_images and model.input_media_field == "images":
            await state.set_state(GenerationStates.waiting_for_images)
            await prompt_for_generation_images(message, edit=edit, model=model, lang=lang)
            return
        await state.set_state(GenerationStates.waiting_for_image)
        await prompt_for_generation_image(message, edit=edit, model=model, lang=lang)
        return
    if required_input_type == "video":
        await state.set_state(GenerationStates.waiting_for_video)
        await prompt_for_generation_video(message, edit=edit, model=model, lang=lang)
        return
    if required_input_type == "audio":
        await state.set_state(GenerationStates.waiting_for_audio)
        await prompt_for_generation_audio(message, edit=edit, model=model, lang=lang)
        return
    await state.set_state(GenerationStates.waiting_for_image)
    await prompt_for_generation_input(message, edit=edit, is_lipsync=model.generation_type == "lipsync", lang=lang)


async def restore_generation_repeat_flow(callback: CallbackQuery, state: FSMContext, session: AsyncSession, generation_id: Any) -> bool:
    lang = await get_event_lang(callback, session)
    generation = await GenerationRepository(session).get_by_id(generation_id)
    if generation is None:
        await callback.answer(build_user_error_message("errors.model_unavailable", lang), show_alert=True)
        return False
    if generation.user_id != callback.from_user.id:
        await callback.answer(build_user_error_message("errors.model_unavailable", lang), show_alert=True)
        return False
    try:
        model = get_generation_model(generation.model_key)
    except ValueError:
        await callback.answer(build_user_error_message("errors.model_unavailable", lang), show_alert=True)
        return False
    if not model.is_enabled:
        await callback.answer(build_user_error_message("errors.model_unavailable", lang), show_alert=True)
        return False
    try:
        user_settings = validate_model_settings(model.key, generation.settings or {})
    except ValueError:
        await callback.answer(build_user_error_message("errors.invalid_model_settings", lang), show_alert=True)
        return False

    await reset_generation_state(state)
    await state.update_data(
        model_key=model.key,
        model_title=model.title,
        model_endpoint=model.endpoint,
        model_generation_type=model.generation_type,
        selected_generation_type=model.generation_type,
        selected_provider=model.provider,
        user_settings=user_settings,
        current_setting_key=None,
        input_image_file_id=None,
        input_media=None,
        input_media_items=[],
        input_media_urls=[],
        input_media_paths=[],
        input_media_file_ids=[],
        input_audio_or_text=None,
        prompt=str(generation.prompt or ""),
        user_language=lang,
    )
    await callback.message.answer(t("generation.repeat_title", lang, model=escape(model.title)), parse_mode="HTML")
    if get_model_required_input_type(model) == "text":
        await send_confirmation_screen(
            message=callback.message,
            state=state,
            session=session,
            telegram_user=callback.from_user,
            edit=False,
        )
        return True
    await prompt_for_repeat_media(callback.message, state, model, edit=False, lang=lang)
    return True


async def show_setting_options(message: Message, state: FSMContext, setting_key: str) -> None:
    """Показать варианты значения конкретной настройки."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    lang = get_state_language(state_data, getattr(message, "from_user", None))
    if not model_key:
        await message.edit_text(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("generation.model_not_selected", lang), lang), reply_markup=None)
        return
    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await message.edit_text(format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("generation.setting_not_found", lang), lang), reply_markup=None)
        return
    user_settings = get_model_state_settings(state_data, model_key)
    current_value = str(user_settings.get(setting_key, model.user_settings[setting_key].default))
    await message.edit_text(
        build_setting_value_text(model, setting_key, current_value, lang),
        reply_markup=build_setting_options_keyboard(model, setting_key, current_value, lang),
        parse_mode="HTML",
    )


async def send_confirmation_screen(
    *,
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    telegram_user,
    edit: bool,
) -> None:
    """Показать экран подтверждения генерации."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    is_lipsync = is_lipsync_generation_state(state_data) or bool(model and model.generation_type == "lipsync")
    required_input_type = get_model_required_input_type(model) if model else "text"
    prompt = (state_data.get("prompt") or "").strip()
    input_media = state_data.get("input_media")
    input_media_items = get_input_media_items(state_data)
    input_audio_or_text = state_data.get("input_audio_or_text")

    user_repo = UserRepository(session)
    user = await user_repo.get_or_create_user_from_telegram(telegram_user)
    lang = get_user_preferred_language(user, telegram_user)

    if is_lipsync:
        prompt = get_input_audio_or_text_display(input_audio_or_text, lang)
        is_complete = bool(
            model_key
            and model
            and state_has_required_media(model, input_media, input_media_items)
            and state_has_required_prompt_or_audio(model, prompt, input_audio_or_text)
        )
    else:
        is_complete = bool(
            model_key
            and model
            and state_has_required_prompt_or_audio(model, prompt, input_audio_or_text)
            and state_has_required_media(model, input_media, input_media_items)
        )

    if not is_complete:
        flow_texts = get_flow_texts(model.generation_type, lang) if model else get_flow_texts("text_to_image", lang)
        if not model:
            error_text = format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang)
        elif is_lipsync:
            error_text = get_lipsync_incomplete_error_text(lang)
        elif not prompt:
            error_text = format_user_error(ErrorCode.E002_MISSING_PROMPT, flow_texts.missing_prompt, lang)
        elif model and model.input_media_field == "images":
            error_text = format_user_error(ErrorCode.E003_MISSING_IMAGE, t("generation.need_min_images", lang, count=model.min_images), lang)
        elif required_input_type == "image":
            error_text = format_user_error(ErrorCode.E003_MISSING_IMAGE, flow_texts.missing_media, lang)
        elif required_input_type == "video":
            error_text = format_user_error(ErrorCode.E004_MISSING_VIDEO, flow_texts.missing_media, lang)
        else:
            error_text = format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.incomplete_generation", lang), lang)
        if edit:
            await message.edit_text(
                error_text,
                reply_markup=None,
            )
        else:
            await message.answer(error_text, reply_markup=get_main_menu_keyboard(lang))
        await reset_generation_state(state)
        return

    user_settings = get_model_state_settings(state_data, model_key)
    await state.update_data(user_language=lang)
    text = build_confirmation_text(model, user_settings, prompt, user.balance, lang)

    await state.set_state(GenerationStates.waiting_for_confirmation)
    if edit:
        await message.edit_text(text, reply_markup=build_generation_confirm_keyboard(lang), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=build_generation_confirm_keyboard(lang), parse_mode="HTML")


@router.message(GenerationStates.waiting_for_images, lambda message: is_localized_button_text(message.text, "common.continue", getattr(message.from_user, "language_code", None)))
async def continue_after_multi_image_upload(message: Message, state: FSMContext):
    """Перейти к prompt после multi-image upload, если набран минимальный набор изображений."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    input_media_urls = get_input_media_urls(state_data)
    lang = get_state_language(state_data, message.from_user)
    if not model:
        await message.answer(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang))
        return
    if len(input_media_urls) < model.min_images:
        await message.answer(
            format_user_error(ErrorCode.E003_MISSING_IMAGE, t("generation.need_min_images", lang, count=model.min_images), lang),
            reply_markup=build_media_upload_reply_keyboard(
                show_continue=True,
                lang=lang,
                show_clear_images=bool(input_media_urls),
            ),
        )
        return
    await state.update_data(input_media={"type": "images", "count": len(input_media_urls)})
    await set_waiting_for_prompt_with_diagnostic(
        state,
        user_id=message.from_user.id,
        incoming_text_type=get_incoming_text_type(message),
    )
    await message.answer(
        get_second_prompt_for_generation_type(model.generation_type, lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


async def log_generation_event(
    generation_id,
    user_id: int,
    model_key: str,
    status: str,
    output_count: int = 0,
) -> None:
    """Логировать только безопасные метаданные генерации."""
    logger.info(
        "generation_id=%s user_id=%s model_key=%s status=%s output_files=%s",
        generation_id,
        user_id,
        model_key,
        status,
        output_count,
    )


def log_balance_event(action: str, user_id: int, amount: int) -> None:
    """Логировать безопасные события изменения баланса без персональных данных."""
    logger.info(
        {
            "action": action,
            "user_id": user_id,
            "amount": amount,
        }
    )


def log_generation_insufficient_balance(
    *,
    user_id: int,
    balance: int,
    required_balance: int,
    model_key: str | None,
) -> None:
    logger.info(
        {
            "action": "generation_insufficient_balance",
            "user_id": user_id,
            "balance": balance,
            "required_balance": required_balance,
            "model_key": model_key,
        }
    )


async def answer_insufficient_balance(
    message: Message,
    *,
    lang: str,
    user_id: int,
    balance: int,
    required_balance: int,
    model_key: str | None,
) -> None:
    log_balance_event("insufficient_balance", user_id, required_balance)
    log_generation_error(ErrorCode.E006_INSUFFICIENT_BALANCE, user_id=user_id, model_key=model_key, status="rejected")
    log_generation_insufficient_balance(
        user_id=user_id,
        balance=balance,
        required_balance=required_balance,
        model_key=model_key,
    )
    await message.answer(
        build_insufficient_balance_message(lang),
        reply_markup=build_insufficient_balance_keyboard(lang),
    )


def log_media_group_event(action: str, message: Message, *, media_group_id: str, **extra: Any) -> None:
    logger.info(
        {
            "action": action,
            "user_id": message.from_user.id,
            "chat_id": message.chat.id,
            "media_group_id": media_group_id,
            **extra,
        }
    )


async def mark_generation_failed(
    *,
    generation_request_id,
    user_id: int,
    model_key: str,
    cost: int,
    error_message: str,
    refund_credit: bool,
    status: str = "failed",
) -> None:
    """Обновить terminal failed-like статус генерации и при необходимости вернуть кредит."""
    async with db_manager.session_factory() as session:
        generation_repo = GenerationRepository(session)
        user_repo = UserRepository(session)
        generation = await generation_repo.get_by_id(generation_request_id)
        previous_status = generation.status if generation is not None else None
        await generation_repo.update_generation_status(
            generation_request_id,
            status,
            error_message=error_message,
        )
        was_active = previous_status in {
            GenerationRequestStatus.CREATED,
            GenerationRequestStatus.PENDING,
            GenerationRequestStatus.PROCESSING,
        }
        should_refund = refund_credit and was_active
        if should_refund:
            refunded = await user_repo.increase_balance(user_id, cost)
            if refunded:
                log_balance_event("balance_refunded", user_id, cost)
        if was_active:
            await user_repo.increment_user_generation_stats(user_id, success=False)
    await log_generation_event(generation_request_id, user_id, model_key, status)


async def mark_generation_completed(
    *,
    generation_request_id,
    user_id: int,
    model_key: str,
    nsfw_flags: Optional[dict[str, Any]],
    output_count: int,
    output_urls: Optional[list[str]] = None,
) -> None:
    """Обновить статус генерации как completed без сохранения output URLs."""
    async with db_manager.session_factory() as session:
        generation_repo = GenerationRepository(session)
        user_repo = UserRepository(session)
        await generation_repo.update_generation_status(
            generation_request_id,
            "completed",
            nsfw_flags=nsfw_flags,
            output_urls=output_urls,
        )
        await user_repo.increment_user_generation_stats(user_id, success=True)
    await log_generation_event(generation_request_id, user_id, model_key, "completed", output_count)


async def safe_send_bot_message(bot, chat_id: int, text: str, reply_markup=None, parse_mode: str | None = None) -> None:
    """Безопасно отправить сообщение пользователю, не роняя background task."""
    if not text or not text.strip():
        logger.warning("Skipped sending empty Telegram message to user")
        return
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as exc:
        logger.exception("Failed to send Telegram message to user: %s", type(exc).__name__)


async def send_generation_summary_message(
    *,
    bot,
    chat_id: int,
    generation_request_ids: list[Any],
    lang: str,
) -> None:
    """Send one final summary for a completed generation batch."""
    async with db_manager.session_factory() as session:
        generations = await GenerationRepository(session).list_by_ids(generation_request_ids)
    generation_batch = build_generation_batch_summary(generations)
    if generation_batch is None:
        return
    if generation_batch.completed_count == 0:
        return
    summary_id = generations[0].id
    await safe_send_bot_message(
        bot,
        chat_id,
        build_generation_summary_message(generation_batch, lang),
        reply_markup=build_generation_summary_keyboard(summary_id, lang),
        parse_mode="HTML",
    )


def get_max_telegram_document_size_bytes() -> int:
    return settings.telegram_max_document_size_mb * 1024 * 1024


def get_safe_telegram_document_size_bytes() -> int:
    return settings.telegram_safe_document_size_mb * 1024 * 1024


def build_large_file_r2_message(short_url: str) -> str:
    return (
        f"⚠️ {t('download.too_large')}\n\n"
        f"{t('download.cloudflare_notice')}\n\n"
        f"🔗 {t('common.download_file')}:\n{short_url}\n\n"
        f"🔒 {t('download.safe_link')}\n\n"
        f"{t('download.browser_notice')}"
    )


async def upload_output_to_r2_and_get_short_url(
    *,
    r2_storage: R2StorageService,
    file_path: str,
    content_type: Optional[str],
    file_size_bytes: Optional[int],
) -> str:
    """Загрузить output-файл в R2 без блокировки event loop и вернуть короткий URL."""
    object_key = await asyncio.to_thread(
        r2_storage.upload_and_get_object_key,
        file_path,
        Path(file_path).name,
        content_type,
    )
    if not object_key or not object_key.strip():
        raise RuntimeError("Cloudflare R2 returned an empty object key")
    short_url = await DownloadLinkService().create_short_download_url(
        object_key,
        filename=Path(file_path).name,
        file_size_bytes=file_size_bytes,
        content_type=content_type,
    )
    if not short_url or not short_url.strip():
        raise RuntimeError("Download link service returned an empty short URL")
    return short_url


async def send_document_with_retry(*, bot, chat_id: int, file_path: str, caption: Optional[str], reply_markup=None) -> None:
    """Отправить документ в Telegram c retry при сетевых ошибках."""
    normalized_filename = Path(file_path).name
    for attempt in range(1, DOCUMENT_SEND_RETRY_COUNT + 2):
        try:
            await bot.send_document(
                chat_id,
                FSInputFile(file_path, filename=normalized_filename),
                caption=caption,
                reply_markup=reply_markup,
                request_timeout=DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS,
            )
            return
        except (TelegramNetworkError, TimeoutError):
            if attempt > DOCUMENT_SEND_RETRY_COUNT:
                raise
            await asyncio.sleep(2 ** (attempt - 1))


def get_output_delivery_kind(content_type: Optional[str]) -> str:
    normalized_content_type = normalize_content_type(content_type)
    if normalized_content_type in {"image/png", "image/jpeg", "image/webp"}:
        return "photo"
    if normalized_content_type in {"video/mp4", "video/webm", "video/quicktime"}:
        return "video"
    return "document"


async def send_photo_output(*, bot, chat_id: int, file_path: str, reply_markup=None) -> None:
    await bot.send_photo(chat_id, FSInputFile(file_path, filename=Path(file_path).name), reply_markup=reply_markup)


async def send_video_output(*, bot, chat_id: int, file_path: str, reply_markup=None) -> None:
    await bot.send_video(
        chat_id,
        FSInputFile(file_path, filename=Path(file_path).name),
        reply_markup=reply_markup,
        request_timeout=DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS,
    )


async def get_user_send_results_as_files(user_id: int) -> bool:
    async with db_manager.session_factory() as session:
        return await UserRepository(session).get_user_delivery_preference(user_id)


async def get_user_keyboard_language(user_id: Optional[int]) -> str:
    if user_id is None:
        return DEFAULT_LANGUAGE
    try:
        async with db_manager.session_factory() as session:
            user = await UserRepository(session).get_user_profile(user_id)
            if user is None:
                return DEFAULT_LANGUAGE
            return get_user_language(user.language_code)
    except Exception:
        return DEFAULT_LANGUAGE


async def send_generation_outputs(
    bot,
    chat_id: int,
    output_urls: list[str],
    user_id: Optional[int] = None,
    delivery_preference: Optional[bool] = None,
    generation_id: Any = None,
    model_key: Optional[str] = None,
    prediction_id: Optional[str] = None,
) -> OutputDeliveryResult:
    """Отправить пользователю результаты генерации с учётом пользовательского способа доставки."""
    delivered_successfully = True
    use_r2 = False
    last_delivery_method = "document"
    last_error_code: Optional[str] = None
    last_error_message: Optional[str] = None
    r2_storage = R2StorageService()
    send_results_as_files = False
    if user_id is not None:
        send_results_as_files = await get_user_send_results_as_files(user_id)
    elif delivery_preference is not None:
        send_results_as_files = delivery_preference
    lang = await get_user_keyboard_language(user_id)
    main_menu_keyboard = get_main_menu_keyboard(lang)
    for output_url in output_urls:
        temp_output_path: Optional[str] = None
        content_type: Optional[str] = None
        file_size_bytes: Optional[int] = None
        try:
            if generation_id is not None and user_id is not None and model_key and prediction_id:
                log_background_generation_event(
                    "output_download_started",
                    generation_id=generation_id,
                    user_id=user_id,
                    chat_id=chat_id,
                    model_key=model_key,
                    prediction_id=prediction_id,
                    outputs_count=len(output_urls),
                )
            temp_output_path, content_type, file_size_bytes = await download_output_file_to_temp(output_url)
            if generation_id is not None and user_id is not None and model_key and prediction_id:
                log_background_generation_event(
                    "output_download_completed",
                    generation_id=generation_id,
                    user_id=user_id,
                    chat_id=chat_id,
                    model_key=model_key,
                    prediction_id=prediction_id,
                    outputs_count=len(output_urls),
                    file_size_bytes=file_size_bytes,
                    content_type=content_type,
                )
            if file_size_bytes is not None and file_size_bytes > get_safe_telegram_document_size_bytes():
                use_r2 = True
                last_delivery_method = "r2"
                if r2_storage.is_configured():
                    try:
                        if generation_id is not None and user_id is not None and model_key and prediction_id:
                            log_background_generation_event(
                                "output_delivery_started",
                                generation_id=generation_id,
                                user_id=user_id,
                                chat_id=chat_id,
                                model_key=model_key,
                                prediction_id=prediction_id,
                                outputs_count=len(output_urls),
                                delivery_method="r2",
                                file_size_bytes=file_size_bytes,
                                content_type=content_type,
                            )
                        short_url = await upload_output_to_r2_and_get_short_url(
                            r2_storage=r2_storage,
                            file_path=temp_output_path,
                            content_type=content_type,
                            file_size_bytes=file_size_bytes,
                        )
                    except Exception:
                        delivered_successfully = False
                        last_error_code = ErrorCode.E009_TELEGRAM_DELIVERY_FAILED
                        last_error_message = "R2 upload failed"
                        log_generation_output_delivery(
                            "r2",
                            user_id=user_id,
                            send_results_as_files=send_results_as_files,
                            content_type=content_type,
                            file_size_bytes=file_size_bytes,
                            status="failed",
                        )
                        await safe_send_bot_message(
                            bot,
                            chat_id,
                            t("download.upload_failed", lang),
                        )
                        if generation_id is not None and user_id is not None and model_key and prediction_id:
                            log_background_generation_event(
                                "output_delivery_failed",
                                generation_id=generation_id,
                                user_id=user_id,
                                chat_id=chat_id,
                                model_key=model_key,
                                prediction_id=prediction_id,
                                outputs_count=len(output_urls),
                                delivery_method="r2",
                                file_size_bytes=file_size_bytes,
                                content_type=content_type,
                                error_code=last_error_code,
                            )
                        continue
                    await safe_send_bot_message(
                        bot,
                        chat_id,
                        build_large_file_r2_message(short_url),
                        reply_markup=main_menu_keyboard,
                    )
                    log_generation_output_delivery(
                        "r2",
                        user_id=user_id,
                        send_results_as_files=send_results_as_files,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                        status="success",
                    )
                    if generation_id is not None and user_id is not None and model_key and prediction_id:
                        log_background_generation_event(
                            "output_delivery_success",
                            generation_id=generation_id,
                            user_id=user_id,
                            chat_id=chat_id,
                            model_key=model_key,
                            prediction_id=prediction_id,
                            outputs_count=len(output_urls),
                            delivery_method="r2",
                            file_size_bytes=file_size_bytes,
                            content_type=content_type,
                        )
                    continue
                delivered_successfully = False
                last_error_code = ErrorCode.E009_TELEGRAM_DELIVERY_FAILED
                last_error_message = "R2 is not configured"
                log_generation_output_delivery(
                    "r2",
                    user_id=user_id,
                    send_results_as_files=send_results_as_files,
                    content_type=content_type,
                    file_size_bytes=file_size_bytes,
                    status="failed",
                )
                await safe_send_bot_message(
                    bot,
                    chat_id,
                    t("download.upload_failed", lang),
                    reply_markup=main_menu_keyboard,
                )
                continue
            delivery_kind = "document" if send_results_as_files else get_output_delivery_kind(content_type)
            last_delivery_method = delivery_kind
            if generation_id is not None and user_id is not None and model_key and prediction_id:
                log_background_generation_event(
                    "output_delivery_started",
                    generation_id=generation_id,
                    user_id=user_id,
                    chat_id=chat_id,
                    model_key=model_key,
                    prediction_id=prediction_id,
                    outputs_count=len(output_urls),
                    delivery_method=delivery_kind,
                    file_size_bytes=file_size_bytes,
                    content_type=content_type,
                )
            if delivery_kind == "photo":
                try:
                    await send_photo_output(bot=bot, chat_id=chat_id, file_path=temp_output_path, reply_markup=main_menu_keyboard)
                    log_generation_output_delivery(
                        "photo",
                        user_id=user_id,
                        send_results_as_files=send_results_as_files,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                        status="success",
                    )
                    if generation_id is not None and user_id is not None and model_key and prediction_id:
                        log_background_generation_event(
                            "output_delivery_success",
                            generation_id=generation_id,
                            user_id=user_id,
                            chat_id=chat_id,
                            model_key=model_key,
                            prediction_id=prediction_id,
                            outputs_count=len(output_urls),
                            delivery_method="photo",
                            file_size_bytes=file_size_bytes,
                            content_type=content_type,
                        )
                    continue
                except Exception:
                    logger.exception("Failed to deliver completed Wavespeed output as photo")
                    log_generation_output_delivery(
                        "photo",
                        user_id=user_id,
                        send_results_as_files=send_results_as_files,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                        status="failed",
                    )
            elif delivery_kind == "video":
                try:
                    await send_video_output(bot=bot, chat_id=chat_id, file_path=temp_output_path, reply_markup=main_menu_keyboard)
                    log_generation_output_delivery(
                        "video",
                        user_id=user_id,
                        send_results_as_files=send_results_as_files,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                        status="success",
                    )
                    if generation_id is not None and user_id is not None and model_key and prediction_id:
                        log_background_generation_event(
                            "output_delivery_success",
                            generation_id=generation_id,
                            user_id=user_id,
                            chat_id=chat_id,
                            model_key=model_key,
                            prediction_id=prediction_id,
                            outputs_count=len(output_urls),
                            delivery_method="video",
                            file_size_bytes=file_size_bytes,
                            content_type=content_type,
                        )
                    continue
                except Exception:
                    logger.exception("Failed to deliver completed Wavespeed output as video")
                    log_generation_output_delivery(
                        "video",
                        user_id=user_id,
                        send_results_as_files=send_results_as_files,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                        status="failed",
                    )

            await send_document_with_retry(
                bot=bot,
                chat_id=chat_id,
                file_path=temp_output_path,
                caption=None,
                reply_markup=main_menu_keyboard,
            )
            log_generation_output_delivery(
                "document",
                user_id=user_id,
                send_results_as_files=send_results_as_files,
                content_type=content_type,
                file_size_bytes=file_size_bytes,
                status="success",
            )
            last_delivery_method = "document"
            if generation_id is not None and user_id is not None and model_key and prediction_id:
                log_background_generation_event(
                    "output_delivery_success",
                    generation_id=generation_id,
                    user_id=user_id,
                    chat_id=chat_id,
                    model_key=model_key,
                    prediction_id=prediction_id,
                    outputs_count=len(output_urls),
                    delivery_method="document",
                    file_size_bytes=file_size_bytes,
                    content_type=content_type,
                )
        except OutputDeliveryTooLargeError:
            delivered_successfully = False
            last_delivery_method = "r2"
            last_error_code = ErrorCode.E009_TELEGRAM_DELIVERY_FAILED
            last_error_message = "Output is too large for Telegram"
            log_generation_error(ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, status="delivery_failed")
            if temp_output_path and r2_storage.is_configured():
                use_r2 = True
                try:
                    short_url = await upload_output_to_r2_and_get_short_url(
                        r2_storage=r2_storage,
                        file_path=temp_output_path,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                    )
                except Exception:
                    last_error_message = "R2 upload failed"
                    log_generation_output_delivery(
                        "r2",
                        user_id=user_id,
                        send_results_as_files=send_results_as_files,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                        status="failed",
                    )
                    await safe_send_bot_message(
                        bot,
                        chat_id,
                        t("download.upload_failed", lang),
                    )
                    continue
                await safe_send_bot_message(bot, chat_id, build_large_file_r2_message(short_url), reply_markup=main_menu_keyboard)
                log_generation_output_delivery(
                    "r2",
                    user_id=user_id,
                    send_results_as_files=send_results_as_files,
                    content_type=content_type,
                    file_size_bytes=file_size_bytes,
                    status="success",
                )
                delivered_successfully = True
                last_error_code = None
                last_error_message = None
                continue
            log_generation_output_delivery(
                "r2",
                user_id=user_id,
                send_results_as_files=send_results_as_files,
                content_type=content_type,
                file_size_bytes=file_size_bytes,
                status="failed",
            )
            await safe_send_bot_message(
                bot,
                chat_id,
                t("download.upload_failed", lang),
                reply_markup=main_menu_keyboard,
            )
        except Exception:
            logger.exception("Failed to deliver completed Wavespeed output as document")
            last_error_code = ErrorCode.E009_TELEGRAM_DELIVERY_FAILED
            last_error_message = "Telegram delivery failed"
            log_generation_error(ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, status="delivery_failed")
            if temp_output_path and r2_storage.is_configured():
                use_r2 = True
                last_delivery_method = "r2"
                try:
                    if generation_id is not None and user_id is not None and model_key and prediction_id:
                        log_background_generation_event(
                            "output_delivery_started",
                            generation_id=generation_id,
                            user_id=user_id,
                            chat_id=chat_id,
                            model_key=model_key,
                            prediction_id=prediction_id,
                            outputs_count=len(output_urls),
                            delivery_method="r2",
                            file_size_bytes=file_size_bytes,
                            content_type=content_type,
                        )
                    short_url = await upload_output_to_r2_and_get_short_url(
                        r2_storage=r2_storage,
                        file_path=temp_output_path,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                    )
                except Exception:
                    delivered_successfully = False
                    last_error_message = "R2 upload failed"
                    log_generation_output_delivery(
                        "r2",
                        user_id=user_id,
                        send_results_as_files=send_results_as_files,
                        content_type=content_type,
                        file_size_bytes=file_size_bytes,
                        status="failed",
                    )
                    await safe_send_bot_message(
                        bot,
                        chat_id,
                        t("download.upload_failed", lang),
                    )
                    continue
                log_generation_output_delivery(
                    "r2",
                    user_id=user_id,
                    send_results_as_files=send_results_as_files,
                    content_type=content_type,
                    file_size_bytes=file_size_bytes,
                    status="success",
                )
                await safe_send_bot_message(
                    bot,
                    chat_id,
                    build_large_file_r2_message(short_url),
                    reply_markup=main_menu_keyboard,
                )
                last_error_code = None
                last_error_message = None
                if generation_id is not None and user_id is not None and model_key and prediction_id:
                    log_background_generation_event(
                        "output_delivery_success",
                        generation_id=generation_id,
                        user_id=user_id,
                        chat_id=chat_id,
                        model_key=model_key,
                        prediction_id=prediction_id,
                        outputs_count=len(output_urls),
                        delivery_method="r2",
                        file_size_bytes=file_size_bytes,
                        content_type=content_type,
                    )
                continue
            delivered_successfully = False
            last_delivery_method = "document"
            log_generation_output_delivery(
                "document",
                user_id=user_id,
                send_results_as_files=send_results_as_files,
                content_type=content_type,
                file_size_bytes=file_size_bytes,
                status="failed",
            )
            await safe_send_bot_message(
                bot,
                chat_id,
                t("download.telegram_failed", lang),
                reply_markup=main_menu_keyboard,
            )
            if generation_id is not None and user_id is not None and model_key and prediction_id:
                log_background_generation_event(
                    "output_delivery_failed",
                    generation_id=generation_id,
                    user_id=user_id,
                    chat_id=chat_id,
                    model_key=model_key,
                    prediction_id=prediction_id,
                    outputs_count=len(output_urls),
                    delivery_method="document",
                    file_size_bytes=file_size_bytes,
                    content_type=content_type,
                    error_code=last_error_code,
                )
        finally:
            await cleanup_temp_output_file(temp_output_path)
    return OutputDeliveryResult(
        success=delivered_successfully,
        method=last_delivery_method,
        error_code=last_error_code,
        error_message=last_error_message,
        use_r2=use_r2,
    )


async def cleanup_temp_output_file(file_path: Optional[str]) -> None:
    """Безопасно удалить временный output-файл и залогировать cleanup."""
    if not file_path:
        return
    path = Path(file_path)
    path.unlink(missing_ok=True)
    logger.info(
        {
            "delivery_method": "cleanup",
            "content_type": None,
            "file_size_bytes": None,
        }
    )


def log_generation_output_delivery(
    delivery_method: str,
    *,
    user_id: Optional[int],
    send_results_as_files: bool,
    content_type: Optional[str],
    file_size_bytes: Optional[int] = None,
    status: str,
) -> None:
    """Логировать только безопасные метаданные доставки результатов генерации."""
    normalized_delivery_method = (
        "document" if send_results_as_files and delivery_method in {"photo", "video", "audio"} else delivery_method
    )
    logger.info(
        {
            "action": "generation_output_delivery",
            "user_id": user_id,
            "send_results_as_files": send_results_as_files,
            "content_type": normalize_content_type(content_type),
            "delivery_method": normalized_delivery_method,
            "file_size": file_size_bytes,
            "status": status,
        }
    )


def log_background_generation_event(
    action: str,
    *,
    generation_id: Any,
    user_id: int,
    chat_id: int,
    model_key: str,
    prediction_id: str,
    outputs_count: int = 0,
    delivery_method: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    content_type: Optional[str] = None,
    error_code: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    """Логировать background delivery без URL, prompt и секретов."""
    logger.info(
        {
            "action": action,
            "generation_id": str(generation_id),
            "user_id": user_id,
            "chat_id": chat_id,
            "model_key": model_key,
            "prediction_id": prediction_id,
            "outputs_count": outputs_count,
            "delivery_method": delivery_method,
            "file_size_bytes": file_size_bytes,
            "content_type": normalize_content_type(content_type),
            "error_code": error_code,
            "status": status,
        }
    )


def normalize_content_type(content_type: Optional[str]) -> Optional[str]:
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    return normalized_content_type or None


def get_output_suffix_and_type(content_type: Optional[str]) -> str:
    """Определить расширение временного output-файла по Content-Type."""
    normalized_content_type = normalize_content_type(content_type)
    if normalized_content_type == "image/png":
        return ".png"
    if normalized_content_type == "image/jpeg":
        return ".jpg"
    if normalized_content_type == "image/webp":
        return ".webp"
    if normalized_content_type == "video/mp4":
        return ".mp4"
    if normalized_content_type == "video/webm":
        return ".webm"
    if normalized_content_type == "video/quicktime":
        return ".mov"
    return ".bin"


def get_content_type_for_path(file_path: str) -> Optional[str]:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mov":
        return "video/quicktime"
    return None


def normalize_filename(original_name: str) -> str:
    """Нормализовать имя output-файла к формату imai-*.ext."""
    raw_name = Path(unquote(original_name or "")).name
    suffix = Path(raw_name).suffix.lower()
    stem = Path(raw_name).stem
    for prefix in ("wavespeed-", "output-"):
        while stem.startswith(prefix):
            stem = stem[len(prefix):]

    cleaned_stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-_")
    if not cleaned_stem:
        cleaned_stem = uuid.uuid4().hex
    if not suffix:
        suffix = ".bin"
    return f"imai-{cleaned_stem}{suffix}"


async def download_file_from_url(url: str, max_size_bytes: Optional[int] = None) -> str:
    """Скачать файл по URL во временную директорию и вернуть путь к нему."""
    temp_path: Optional[str] = None
    bytes_written = 0
    try:
        timeout = aiohttp.ClientTimeout(total=OUTPUT_DOWNLOAD_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type")
                content_length_header = response.headers.get("content-length")
                if content_length_header is not None:
                    try:
                        content_length = int(content_length_header)
                    except ValueError:
                        content_length = None
                    else:
                        if max_size_bytes is not None and content_length > max_size_bytes:
                            raise OutputDeliveryTooLargeError()

                suffix = get_output_suffix_and_type(content_type)
                original_filename = Path(unquote(urlparse(url).path)).name
                normalized_filename = normalize_filename(original_filename)
                if Path(normalized_filename).suffix == ".bin" and suffix != ".bin":
                    normalized_filename = f"{Path(normalized_filename).stem}{suffix}"

                temp_dir = Path(tempfile.gettempdir())
                candidate_path = temp_dir / normalized_filename
                if candidate_path.exists():
                    candidate_path = temp_dir / f"imai-{uuid.uuid4().hex}{Path(normalized_filename).suffix}"
                temp_path = str(candidate_path)
                temp_file = open(candidate_path, "wb")
                try:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        bytes_written += len(chunk)
                        if max_size_bytes is not None and bytes_written > max_size_bytes:
                            raise OutputDeliveryTooLargeError()
                        temp_file.write(chunk)
                finally:
                    temp_file.close()
        return temp_path
    except Exception:
        logger.exception("Failed to download file from URL")
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)
        raise


async def download_output_file_to_temp(output_url: str) -> tuple[str, Optional[str], Optional[int]]:
    """Скачать output-файл во временный файл для последующей отправки в Telegram."""
    temp_path = await download_file_from_url(output_url)
    file_path = Path(temp_path)
    return temp_path, get_content_type_for_path(temp_path), file_path.stat().st_size


def log_background_task_exception(task: asyncio.Task) -> None:
    """Забрать исключение фоновой задачи, чтобы не было unhandled task exception."""
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("Background generation task was cancelled")
    except Exception as exc:
        logger.exception("Background generation task failed: %s", exc)


async def cleanup_generation_file(temp_input_path: Optional[str] | list[str]) -> None:
    """Удалить временные входные файлы после завершения сценария."""
    if isinstance(temp_input_path, list):
        for path in temp_input_path:
            if path:
                Path(path).unlink(missing_ok=True)
        return
    if temp_input_path:
        Path(temp_input_path).unlink(missing_ok=True)


async def reset_generation_state(state: FSMContext) -> None:
    """Сбросить FSM генерации и промежуточные данные."""
    await cleanup_state_media(state)
    await state.clear()


def normalize_generation_state(state_value: Any) -> Optional[str]:
    if state_value is None:
        return None
    return getattr(state_value, "state", state_value)


def is_generation_flow_state(state_value: Any) -> bool:
    return normalize_generation_state(state_value) in GENERATION_FLOW_STATE_NAMES


async def reset_generation_flow(state: FSMContext, reason: str) -> None:
    """Безопасно очистить FSM generation flow и временные файлы."""
    current_state = normalize_generation_state(await state.get_state())
    state_data = await state.get_data()
    await cleanup_state_media(state)
    await state.clear()
    log_generation_diagnostic(
        action="generation_flow_reset",
        user_id=state_data.get("last_user_id"),
        state_value=current_state,
        state_data=state_data,
        incoming_text_type="system",
        extra={"reason": reason},
    )


async def poll_generation_result(
    *,
    bot,
    user_id: int,
    chat_id: int,
    generation_request_id,
    prediction_id: str,
    model_key: str,
    cost: int,
    temp_input_path: Optional[str] | list[str],
) -> None:
    """Дождаться terminal результата, затем удалить временный input media."""
    await _run_single_generation_request(
        bot=bot,
        user_id=user_id,
        chat_id=chat_id,
        generation_request_id=generation_request_id,
        prediction_id=prediction_id,
        model_key=model_key,
        cost=cost,
        temp_input_path=temp_input_path,
        cleanup_inputs=True,
        use_partial_failure_message=False,
    )
    lang = await get_user_keyboard_language(user_id)
    await send_generation_summary_message(
        bot=bot,
        chat_id=chat_id,
        generation_request_ids=[generation_request_id],
        lang=lang,
    )


async def submit_generation_request(
    *,
    generation_request_id,
    user_id: int,
    model_key: str,
    payload: dict[str, Any],
) -> str:
    """Submit generation to Wavespeed and persist the prediction id before polling starts."""
    wavespeed = WavespeedService()
    try:
        submit_result = await wavespeed.submit_generation(
            model_key=model_key,
            payload=payload,
        )
        prediction_id = submit_result.prediction_id

        async with db_manager.session_factory() as session:
            generation_repo = GenerationRepository(session)
            await generation_repo.update_generation_status(
                generation_request_id,
                status="processing",
                wavespeed_prediction_id=prediction_id,
            )

        await log_generation_event(generation_request_id, user_id, model_key, "processing")
        return prediction_id
    finally:
        await wavespeed.close()


async def _run_single_generation_request(
    *,
    bot,
    user_id: int,
    chat_id: int,
    generation_request_id,
    prediction_id: str,
    model_key: str,
    cost: int,
    temp_input_path: Optional[str] | list[str],
    cleanup_inputs: bool,
    use_partial_failure_message: bool,
) -> None:
    wavespeed = WavespeedService()
    result: Optional[WavespeedResult] = None
    try:
        log_background_generation_event(
            "background_task_started",
            generation_id=generation_request_id,
            user_id=user_id,
            chat_id=chat_id,
            model_key=model_key,
            prediction_id=prediction_id,
        )
        log_background_generation_event(
            "background_generation_started",
            generation_id=generation_request_id,
            user_id=user_id,
            chat_id=chat_id,
            model_key=model_key,
            prediction_id=prediction_id,
        )
        log_background_generation_event(
            "background_polling_started",
            generation_id=generation_request_id,
            user_id=user_id,
            chat_id=chat_id,
            model_key=model_key,
            prediction_id=prediction_id,
        )
        result = await wavespeed.poll_until_complete(
            prediction_id,
            timeout_seconds=settings.wavespeed_poll_timeout_seconds,
            generation_id=generation_request_id,
        )
        outputs = list(result.outputs or [])
        log_background_generation_event(
            "poll_result_received",
            generation_id=generation_request_id,
            user_id=user_id,
            chat_id=chat_id,
            model_key=model_key,
            prediction_id=prediction_id,
            outputs_count=len(outputs),
            status=getattr(result, "status", None),
        )
        log_background_generation_event(
            "background_polling_completed",
            generation_id=generation_request_id,
            user_id=user_id,
            chat_id=chat_id,
            model_key=model_key,
            prediction_id=prediction_id,
            outputs_count=len(outputs),
        )
        log_background_generation_event(
            "background_outputs_received",
            generation_id=generation_request_id,
            user_id=user_id,
            chat_id=chat_id,
            model_key=model_key,
            prediction_id=prediction_id,
            outputs_count=len(outputs),
        )
        if not outputs:
            log_generation_error(
                ErrorCode.E010_INTERNAL_ERROR,
                generation_id=generation_request_id,
                user_id=user_id,
                model_key=model_key,
                status="failed",
                details="completed_without_outputs",
            )
            await mark_generation_failed(
                generation_request_id=generation_request_id,
                user_id=user_id,
                model_key=model_key,
                cost=cost,
                error_message="Wavespeed completed without output URLs",
                refund_credit=True,
            )
            lang = await get_user_keyboard_language(user_id)
            await safe_send_bot_message(
                bot,
                chat_id,
                build_empty_outputs_failed_message(lang),
                reply_markup=get_main_menu_keyboard(lang),
            )
            return

        try:
            log_background_generation_event(
                "send_outputs_called",
                generation_id=generation_request_id,
                user_id=user_id,
                chat_id=chat_id,
                model_key=model_key,
                prediction_id=prediction_id,
                outputs_count=len(outputs),
            )
            delivery_result = await send_generation_outputs(
                bot,
                chat_id,
                outputs,
                user_id,
                generation_id=generation_request_id,
                model_key=model_key,
                prediction_id=prediction_id,
            )
            log_background_generation_event(
                "send_outputs_finished",
                generation_id=generation_request_id,
                user_id=user_id,
                chat_id=chat_id,
                model_key=model_key,
                prediction_id=prediction_id,
                outputs_count=len(outputs),
                delivery_method=delivery_result.method,
                error_code=delivery_result.error_code,
                status="success" if delivery_result.success else "failed",
            )
        except Exception as exc:
            logger.exception(
                {
                    "action": "background_generation_task_failed",
                    "generation_id": str(generation_request_id),
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "model_key": model_key,
                    "prediction_id": prediction_id,
                    "outputs_count": len(outputs),
                    "delivery_method": None,
                    "file_size_bytes": None,
                    "content_type": None,
                }
            )
            await mark_generation_failed(
                generation_request_id=generation_request_id,
                user_id=user_id,
                model_key=model_key,
                cost=cost,
                error_message=f"Output delivery crashed: {type(exc).__name__}",
                refund_credit=True,
            )
            lang = await get_user_keyboard_language(user_id)
            await safe_send_bot_message(
                bot,
                chat_id,
                build_generated_but_delivery_failed_message(lang),
                reply_markup=get_main_menu_keyboard(lang),
            )
            return

        if not delivery_result.success:
            log_generation_error(
                ErrorCode.E009_TELEGRAM_DELIVERY_FAILED,
                generation_id=generation_request_id,
                user_id=user_id,
                model_key=model_key,
                status="delivery_failed",
                details=delivery_result.error_message,
            )
            await mark_generation_failed(
                generation_request_id=generation_request_id,
                user_id=user_id,
                model_key=model_key,
                cost=cost,
                error_message=delivery_result.error_message or "Telegram delivery failed",
                refund_credit=True,
                status="delivery_failed",
            )
            lang = await get_user_keyboard_language(user_id)
            await safe_send_bot_message(
                bot,
                chat_id,
                build_telegram_delivery_failed_refund_message(lang),
                reply_markup=get_main_menu_keyboard(lang),
            )
            return

        await mark_generation_completed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            nsfw_flags=result.raw_response.get("nsfw_flags"),
            output_count=len(outputs),
            output_urls=outputs,
        )
    except WavespeedTimeoutError as exc:
        logger.exception("Wavespeed timeout while polling generation result")
        log_generation_error(
            ErrorCode.E008_WAVESPEED_TIMEOUT,
            generation_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            status="timeout",
        )
        await mark_generation_failed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            cost=cost,
            error_message=sanitize_external_error_message(exc.user_message) or "Wavespeed polling timed out",
            refund_credit=True,
            status="timeout",
        )
        lang = await get_user_keyboard_language(user_id)
        await safe_send_bot_message(
            bot,
            chat_id,
            build_partial_generation_failed_message(lang) if use_partial_failure_message else get_user_friendly_error_message(exc, result, lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
    except WavespeedFailedError as exc:
        logger.exception("Wavespeed failed while polling generation result")
        result = getattr(exc, "result", result)
        safe_error_message = None
        if result is not None:
            safe_error_message = sanitize_external_error_message(result.error)
        if not safe_error_message:
            safe_error_message = sanitize_external_error_message(exc.user_message)
        log_generation_error(
            ErrorCode.E007_WAVESPEED_FAILED,
            generation_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            status="failed",
            details=safe_error_message,
        )
        await mark_generation_failed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            cost=cost,
            error_message=safe_error_message or t("errors.generation_failed_refund", await get_user_keyboard_language(user_id)),
            refund_credit=True,
        )
        lang = await get_user_keyboard_language(user_id)
        await safe_send_bot_message(
            bot,
            chat_id,
            build_partial_generation_failed_message(lang) if use_partial_failure_message else get_user_friendly_error_message(exc, result, lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
    except WavespeedNetworkError as exc:
        logger.exception("Wavespeed network error while polling generation result")
        log_generation_error(
            ErrorCode.E010_INTERNAL_ERROR,
            generation_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            status="failed",
            details=sanitize_external_error_message(exc.user_message),
        )
        await mark_generation_failed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            cost=cost,
            error_message=exc.user_message,
            refund_credit=True,
        )
        lang = await get_user_keyboard_language(user_id)
        await safe_send_bot_message(
            bot,
            chat_id,
            build_partial_generation_failed_message(lang) if use_partial_failure_message else get_user_friendly_error_message(exc, result, lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
    except Exception as exc:
        logger.exception(
            {
                "action": "background_generation_task_failed",
                "generation_id": str(generation_request_id),
                "user_id": user_id,
                "chat_id": chat_id,
                "model_key": model_key,
                "prediction_id": prediction_id,
                "outputs_count": len(result.outputs) if result is not None else 0,
                "delivery_method": None,
                "file_size_bytes": None,
                "content_type": None,
            }
        )
        log_generation_error(
            ErrorCode.E010_INTERNAL_ERROR,
            generation_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            status="failed",
        )
        await mark_generation_failed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            cost=cost,
            error_message=t("errors.finish_generation_failed", await get_user_keyboard_language(user_id)),
            refund_credit=True,
        )
        lang = await get_user_keyboard_language(user_id)
        await safe_send_bot_message(
            bot,
            chat_id,
            build_partial_generation_failed_message(lang) if use_partial_failure_message else get_user_friendly_error_message(exc, result, lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
    finally:
        if cleanup_inputs:
            await cleanup_generation_file(temp_input_path)
        await wavespeed.close()
        BACKGROUND_GENERATIONS.pop(generation_request_id, None)
        log_background_generation_event(
            "background_generation_finished",
            generation_id=generation_request_id,
            user_id=user_id,
            chat_id=chat_id,
            model_key=model_key,
            prediction_id=prediction_id,
            outputs_count=len(result.outputs) if result is not None and result.outputs is not None else 0,
        )


async def poll_generation_results_batch(
    *,
    bot,
    user_id: int,
    chat_id: int,
    generation_predictions: list[tuple[Any, str]],
    model_key: str,
    cost: int,
    temp_input_path: Optional[str] | list[str],
    generation_costs: Optional[Mapping[Any, int]] = None,
) -> None:
    async def _run_child(generation_request_id, prediction_id: str) -> None:
        await _run_single_generation_request(
            bot=bot,
            user_id=user_id,
            chat_id=chat_id,
            generation_request_id=generation_request_id,
            prediction_id=prediction_id,
            model_key=model_key,
            cost=(generation_costs or {}).get(generation_request_id, cost),
            temp_input_path=None,
            cleanup_inputs=False,
            use_partial_failure_message=True,
        )

    try:
        results = await asyncio.gather(
            *(_run_child(generation_request_id, prediction_id) for generation_request_id, prediction_id in generation_predictions),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.exception("Batch generation task failed unexpectedly: %s", result)
    finally:
        await cleanup_generation_file(temp_input_path)
        lang = await get_user_keyboard_language(user_id)
        await send_generation_summary_message(
            bot=bot,
            chat_id=chat_id,
            generation_request_ids=[generation_request_id for generation_request_id, _ in generation_predictions],
            lang=lang,
        )
        for generation_request_id, _ in generation_predictions:
            BACKGROUND_GENERATIONS.pop(generation_request_id, None)


async def recover_background_generations(bot) -> int:
    """Восстановить polling активных генераций после рестарта процесса."""
    recovered_count = 0
    async with db_manager.session_factory() as session:
        generation_repo = GenerationRepository(session)
        recoverable_generations = await generation_repo.list_recoverable_generations()

    for generation in recoverable_generations:
        if generation.id in BACKGROUND_GENERATIONS:
            continue
        if not generation.wavespeed_prediction_id:
            continue
        chat_id = generation.chat_id or generation.user_id
        log_background_generation_event(
            "background_task_scheduled",
            generation_id=generation.id,
            user_id=generation.user_id,
            chat_id=chat_id,
            model_key=generation.model_key,
            prediction_id=generation.wavespeed_prediction_id,
        )
        task = asyncio.create_task(
            poll_generation_result(
                bot=bot,
                user_id=generation.user_id,
                chat_id=chat_id,
                generation_request_id=generation.id,
                prediction_id=generation.wavespeed_prediction_id,
                model_key=generation.model_key,
                cost=generation.cost,
                temp_input_path=None,
            )
        )
        task.add_done_callback(log_background_task_exception)
        BACKGROUND_GENERATIONS[generation.id] = {
            "task": task,
            "generation_request_id": generation.id,
            "generation_request_ids": [generation.id],
            "recovered": True,
        }
        recovered_count += 1
        log_background_generation_event(
            "background_generation_recovered",
            generation_id=generation.id,
            user_id=generation.user_id,
            chat_id=chat_id,
            model_key=generation.model_key,
            prediction_id=generation.wavespeed_prediction_id,
        )

    return recovered_count


@router.message(lambda msg: is_localized_button_text(msg.text, "main.generations", getattr(msg.from_user, "language_code", None)))
async def show_generation_menu(message: Message, state: FSMContext, session: Optional[AsyncSession] = None):
    """Показать меню генерации."""
    try:
        lang = await get_event_lang(message, session)
        current_state = await state.get_state()

        if is_generation_flow_state(current_state):
            await reset_generation_flow(state, reason="main_menu_generations")
            await message.answer(
                t("generation.scenario_reset", lang),
                reply_markup=get_main_menu_keyboard(lang),
            )
        elif current_state is not None:
            await state.clear()

        await reset_generation_state(state)
        await state.set_state(GenerationStates.choosing_generation_type)
        await state.update_data(selected_generation_type=None, selected_provider=None, user_language=lang)
        await render_models_screen(message, lang)
        
        logger.debug(f"Generation menu shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_generation_menu: %s", e)
        lang = await get_event_lang(message, session)
        await message.answer(build_user_error_message("main.menu_open_error", lang), reply_markup=build_error_keyboard("main.menu_open_error", lang))


@router.callback_query(lambda cb: cb.data == "gen:retry")
async def recover_to_generation_menu(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Open the generation menu from an Error UX recovery button."""
    lang = await get_event_lang(callback, session)
    await reset_generation_state(state)
    await state.set_state(GenerationStates.choosing_generation_type)
    await state.update_data(selected_generation_type=None, selected_provider=None, user_language=lang)
    if callback.message is not None:
        await render_models_screen(callback.message, lang)
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(MODEL_PREFIX))
async def choose_generation_model(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Выбрать модель для генерации."""
    log_generation_callback(callback)
    model_token = callback.data.removeprefix(MODEL_PREFIX)
    state_data = await state.get_data()
    model_key = resolve_model_key_from_token(get_selected_models_for_state(state_data), model_token) or model_token
    model = get_generation_model(model_key)
    lang = await get_event_lang(callback, session)
    await state.set_state(GenerationStates.choosing_settings)
    await state.update_data(
        model_key=model.key,
        model_title=model.title,
        model_endpoint=model.endpoint,
        model_generation_type=model.generation_type,
        user_settings=get_default_settings(model.key),
        current_setting_key=None,
        input_image_file_id=None,
        input_media=None,
        input_media_items=[],
        input_media_urls=[],
        input_media_paths=[],
        input_media_file_ids=[],
        input_audio_or_text=None,
        prompt=None,
        user_language=lang,
    )
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(GENERATION_REPEAT_PREFIX))
async def repeat_generation_from_summary(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Restore a generation flow from summary repeat metadata."""
    log_generation_callback(callback)
    raw_generation_id = callback.data.removeprefix(GENERATION_REPEAT_PREFIX)
    try:
        generation_id = uuid.UUID(raw_generation_id)
    except (TypeError, ValueError):
        await callback.answer(build_user_error_message("errors.model_unavailable", await get_event_lang(callback, session)), show_alert=True)
        return
    restored = await restore_generation_repeat_flow(callback, state, session, generation_id)
    if restored:
        await callback.answer()


@router.callback_query(F.data == PAGINATION_NOOP_CALLBACK)
async def ignore_pagination_noop(callback: CallbackQuery):
    """Acknowledge disabled pagination label clicks."""
    await callback.answer()


@router.callback_query(F.data.startswith(GENERATION_SECTION_PREFIX))
async def choose_generation_section(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Выбрать раздел генерации и показать список моделей."""
    log_generation_callback(callback)
    lang = await get_event_lang(callback, session)
    generation_type = callback.data.removeprefix(GENERATION_SECTION_PREFIX)
    if is_all_models_category(generation_type):
        await state.set_state(GenerationStates.choosing_provider)
        await state.update_data(selected_generation_type=LEGACY_ALL_MODELS_CATEGORY, selected_provider=None, selected_model_page=0, current_screen="providers", user_language=lang)
        await render_provider_screen(callback.message, edit=True, lang=lang)
        await callback.answer()
        return
    models = list_models_by_type(generation_type)
    await state.set_state(GenerationStates.choosing_generation_type)
    await state.update_data(selected_generation_type=generation_type, selected_provider=None, selected_model_page=0, user_language=lang)
    if not models:
        await callback.message.edit_text(
            t("generation.no_models_in_section", lang),
            reply_markup=build_models_keyboard([], BACK_TO_SECTIONS, lang),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await render_model_list_screen(
        callback.message,
        models=models,
        edit=True,
        heading=f"{t('generation.choose_model', lang)}:",
        back_callback=BACK_TO_SECTIONS,
        page_callback_builder=lambda target_page: f"{MODELS_PAGE_PREFIX}{generation_type}:{target_page}",
        lang=lang,
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(MODELS_PAGE_PREFIX))
async def show_generation_models_page(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Показать страницу моделей выбранного раздела."""
    log_generation_callback(callback)
    generation_type, page = parse_models_page(callback.data)
    lang = await get_event_lang(callback, session)
    models = list_models_by_type(generation_type)
    if not models:
        await callback.answer(build_user_error_message("generation.no_models_in_section", lang), show_alert=True)
        return

    await state.set_state(GenerationStates.choosing_generation_type)
    await state.update_data(selected_generation_type=generation_type, selected_provider=None, selected_model_page=page, user_language=lang)
    await render_model_list_screen(
        callback.message,
        models=models,
        edit=True,
        heading=f"{t('generation.choose_model', lang)}:",
        back_callback=BACK_TO_SECTIONS,
        page=page,
        page_callback_builder=lambda target_page: f"{MODELS_PAGE_PREFIX}{generation_type}:{target_page}",
        lang=lang,
    )
    await callback.answer()


@router.callback_query(F.data == GENERATION_ALL)
async def show_all_generation_providers(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Показать список провайдеров для All List."""
    log_generation_callback(callback)
    lang = await get_event_lang(callback, session)
    await state.set_state(GenerationStates.choosing_provider)
    await state.update_data(selected_generation_type=LEGACY_ALL_MODELS_CATEGORY, selected_provider=None, selected_model_page=0, current_screen="providers", user_language=lang)
    await render_provider_screen(callback.message, edit=True, lang=lang)
    await callback.answer()


@router.callback_query(F.data == BACK_TO_SECTIONS)
async def back_to_generation_sections(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Вернуться к выбору раздела генерации."""
    log_generation_callback(callback)
    await state.set_state(GenerationStates.choosing_generation_type)
    lang = await get_event_lang(callback, session)
    await state.update_data(selected_generation_type=None, selected_provider=None, selected_model_page=0, user_language=lang)
    await callback.message.edit_text(
        build_generation_types_screen_text(lang),
        reply_markup=build_generation_sections_keyboard(lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(PROVIDERS_PAGE_PREFIX))
async def show_generation_providers_page(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Показать страницу списка провайдеров."""
    log_generation_callback(callback)
    lang = await get_event_lang(callback, session)
    page = parse_page(callback.data.removeprefix(PROVIDERS_PAGE_PREFIX))
    await state.set_state(GenerationStates.choosing_provider)
    await state.update_data(selected_generation_type=LEGACY_ALL_MODELS_CATEGORY, selected_provider=None, selected_model_page=0, current_screen="providers", user_language=lang)
    await render_provider_screen(callback.message, edit=True, page=page, lang=lang)
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(PROVIDER_PREFIX))
async def choose_provider(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Выбрать провайдера и показать его модели."""
    log_generation_callback(callback)
    lang = await get_event_lang(callback, session)
    provider, page = parse_provider_page(callback.data)
    if provider not in list_providers():
        await callback.answer(build_user_error_message("generation.provider_unavailable", lang), show_alert=True)
        return
    models = list_models_by_provider(provider)
    if not models:
        await state.set_state(GenerationStates.choosing_provider)
        await state.update_data(selected_generation_type=LEGACY_ALL_MODELS_CATEGORY, selected_provider=None, user_language=lang)
        await callback.message.edit_text(
            t("generation.no_models_in_provider", lang),
            reply_markup=build_providers_keyboard(lang),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.set_state(GenerationStates.choosing_provider)
    await state.update_data(selected_generation_type=LEGACY_ALL_MODELS_CATEGORY, selected_provider=provider, selected_model_page=page, user_language=lang)
    await render_model_list_screen(
        callback.message,
        models=models,
        edit=True,
        heading=f"{t('generation.choose_model', lang)}:",
        back_callback=BACK_TO_PROVIDERS,
        page=page,
        page_callback_builder=lambda target_page: f"{PROVIDER_PREFIX}{provider}:{target_page}",
        lang=lang,
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == SETTINGS_BACK_MODELS)
async def back_to_generation_models(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Вернуться к предыдущему экрану выбора модели."""
    log_generation_callback(callback)
    state_data = await state.get_data()
    lang = await get_event_lang(callback, session)
    selected_provider = state_data.get("selected_provider")
    selected_generation_type = state_data.get("selected_generation_type")
    selected_model_page = parse_page(state_data.get("selected_model_page"))

    for key in (
        "model_key",
        "model_title",
        "model_endpoint",
        "model_generation_type",
        "user_settings",
        "current_setting_key",
        "input_image_file_id",
        "input_media",
        "input_media_items",
        "input_media_urls",
        "input_media_paths",
        "input_media_file_ids",
        "input_audio_or_text",
        "prompt",
    ):
        await state.update_data(**{key: None})

    if selected_provider:
        await state.set_state(GenerationStates.choosing_provider)
        await render_model_list_screen(
            callback.message,
            models=list_models_by_provider(str(selected_provider)),
            edit=True,
            heading=f"{t('generation.choose_model', lang)}:",
            back_callback=BACK_TO_PROVIDERS,
            page=selected_model_page,
            page_callback_builder=lambda target_page: f"{PROVIDER_PREFIX}{selected_provider}:{target_page}",
            lang=lang,
        )
        await callback.answer()
        return

    if selected_generation_type and not is_all_models_category(selected_generation_type):
        await state.set_state(GenerationStates.choosing_generation_type)
        await render_model_list_screen(
            callback.message,
            models=list_models_by_type(str(selected_generation_type)),
            edit=True,
            heading=f"{t('generation.choose_model', lang)}:",
            back_callback=BACK_TO_SECTIONS,
            page=selected_model_page,
            page_callback_builder=lambda target_page: f"{MODELS_PAGE_PREFIX}{selected_generation_type}:{target_page}",
            lang=lang,
        )
        await callback.answer()
        return

    await back_to_generation_sections(callback, state, session)


@router.callback_query(F.data == BACK_TO_PROVIDERS)
async def back_to_generation_providers(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Вернуться к списку провайдеров."""
    log_generation_callback(callback)
    lang = await get_event_lang(callback, session)
    await state.set_state(GenerationStates.choosing_provider)
    await state.update_data(selected_generation_type=LEGACY_ALL_MODELS_CATEGORY, selected_provider=None, selected_model_page=0, user_language=lang)
    await render_provider_screen(callback.message, edit=True, lang=lang)
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(SETTINGS_OPEN_PREFIX))
async def open_setting_selector(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Открыть выбор значения настройки модели."""
    log_generation_callback(callback)
    state_data = await state.get_data()
    lang = get_state_language(state_data, callback.from_user)
    setting_key = callback.data.removeprefix(SETTINGS_OPEN_PREFIX)
    if not setting_key:
        await callback.answer(build_user_error_message("generation.setting_not_found", lang), show_alert=True)
        return
    model_key = state_data.get("model_key")
    if not model_key:
        await callback.message.edit_text(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("generation.model_not_selected", lang), lang), reply_markup=None)
        await callback.answer()
        return
    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await callback.answer(build_user_error_message("generation.setting_not_found", lang), show_alert=True)
        return
    setting = model.user_settings[setting_key]
    await state.update_data(current_setting_key=setting_key)
    if setting.type in FREEFORM_SETTING_TYPES:
        user_settings = get_model_state_settings(state_data, model_key)
        current_value = str(user_settings.get(setting_key, setting.default))
        next_state = GenerationStates.waiting_for_setting_number if setting.type in NUMERIC_SETTING_TYPES else GenerationStates.waiting_for_setting_text
        await state.set_state(next_state)
        await callback.message.edit_text(
            build_setting_value_text(model, setting_key, current_value, lang),
            reply_markup=build_setting_input_back_keyboard(lang),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    await state.set_state(GenerationStates.choosing_setting_value)
    await show_setting_options(callback.message, state, setting_key)
    await callback.answer()


@router.callback_query(lambda cb: cb.data == SETTINGS_BACK_PREFIX)
async def back_to_settings(callback: CallbackQuery, state: FSMContext):
    """Вернуться на экран настроек модели."""
    log_generation_callback(callback)
    await state.set_state(GenerationStates.choosing_settings)
    await state.update_data(current_setting_key=None)
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.message(GenerationStates.waiting_for_setting_text, lambda message: is_localized_button_text(message.text, "common.back_to_settings", getattr(message.from_user, "language_code", None)))
async def back_to_settings_from_text_setting(message: Message, state: FSMContext):
    """Вернуться с текстового ввода настройки к экрану настроек модели."""
    await state.set_state(GenerationStates.choosing_settings)
    await state.update_data(current_setting_key=None)
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    await message.answer(t("generation.back_to_model_settings", lang), reply_markup=get_main_menu_keyboard(lang))
    await render_settings_screen_message(message, state, edit=False)


@router.message(GenerationStates.waiting_for_setting_number, lambda message: is_localized_button_text(message.text, "common.back_to_settings", getattr(message.from_user, "language_code", None)))
async def back_to_settings_from_number_setting(message: Message, state: FSMContext):
    """Вернуться с числового ввода настройки к экрану настроек модели."""
    await back_to_settings_from_text_setting(message, state)


@router.message(GenerationStates.waiting_for_setting_number)
async def process_number_setting_value(message: Message, state: FSMContext):
    """Сохранить числовое значение настройки и вернуть пользователя к настройкам модели."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    setting_key = state_data.get("current_setting_key")
    lang = get_state_language(state_data, message.from_user)
    if not model_key or not setting_key:
        await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.setting_not_selected", lang), lang))
        return

    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.setting_unavailable", lang), lang))
        return

    setting = model.user_settings[str(setting_key)]
    if message_contains_file(message):
        await message.answer(t("errors.number_setting_media_sent", lang))
        return

    raw_text = (message.text or "").strip()
    if raw_text == "-":
        value = ""
    else:
        value, error_text = validate_numeric_setting_input(setting, raw_text, lang)
        if error_text:
            await message.answer(error_text)
            return

    user_settings = get_model_state_settings(state_data, str(model_key))
    trial_settings = dict(user_settings)
    trial_settings[str(setting_key)] = value
    try:
        validate_model_settings(str(model_key), trial_settings)
    except ValueError:
        await message.answer(t("generation.invalid_value", lang))
        return
    user_settings[str(setting_key)] = value
    await state.update_data(user_settings=user_settings, current_setting_key=None)
    await state.set_state(GenerationStates.choosing_settings)
    await message.answer(t("generation.value_saved", lang), reply_markup=get_main_menu_keyboard(lang))
    await render_settings_screen_message(message, state, edit=False)


@router.message(GenerationStates.waiting_for_setting_text)
async def process_text_setting_value(message: Message, state: FSMContext):
    """Сохранить текстовое значение настройки и вернуть пользователя к настройкам модели."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    setting_key = state_data.get("current_setting_key")
    lang = get_state_language(state_data, message.from_user)
    if not model_key or not setting_key:
        await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.setting_not_selected", lang), lang))
        return

    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.setting_unavailable", lang), lang))
        return

    if message_contains_file(message):
        await message.answer(
            format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, t("errors.setting_text_required", lang), lang),
        )
        return

    raw_text = (message.text or "").strip()
    value = "" if raw_text in {"", "-"} else raw_text
    user_settings = get_model_state_settings(state_data, model_key)
    trial_settings = dict(user_settings)
    trial_settings[str(setting_key)] = value
    try:
        validate_model_settings(model_key, trial_settings)
    except ValueError as exc:
        await message.answer(format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, t("generation.invalid_value", lang), lang))
        return
    user_settings[str(setting_key)] = value
    await state.update_data(user_settings=user_settings, current_setting_key=None)
    await state.set_state(GenerationStates.choosing_settings)
    await message.answer(t("generation.value_saved", lang), reply_markup=get_main_menu_keyboard(lang))
    await render_settings_screen_message(message, state, edit=False)


@router.callback_query(lambda cb: cb.data.startswith(SETTINGS_VALUE_PREFIX))
async def choose_setting_value(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Сохранить выбранное значение настройки и вернуться к экрану настроек."""
    log_generation_callback(callback)
    state_data = await state.get_data()
    lang = get_state_language(state_data, callback.from_user)
    setting_payload = callback.data.removeprefix(SETTINGS_VALUE_PREFIX)
    if ":" not in setting_payload:
        await callback.answer(build_user_error_message("generation.invalid_value", lang), show_alert=True)
        return
    setting_key, option_index_raw = setting_payload.rsplit(":", 1)
    if not option_index_raw.isdigit():
        await callback.answer(build_user_error_message("generation.invalid_value", lang), show_alert=True)
        return
    model_key = state_data.get("model_key")
    if not model_key:
        await callback.message.edit_text(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("generation.model_not_selected", lang), lang), reply_markup=None)
        await callback.answer()
        return

    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await callback.answer(build_user_error_message("generation.setting_not_found", lang), show_alert=True)
        return

    user_settings = get_model_state_settings(state_data, model_key)
    option_index = int(option_index_raw)
    options = model.user_settings[setting_key].options
    if option_index < 0 or option_index >= len(options):
        await callback.answer(build_user_error_message("generation.invalid_value", lang), show_alert=True)
        return
    selected_value = str(options[option_index].value)
    user_settings[setting_key] = selected_value
    await state.update_data(user_settings=user_settings, current_setting_key=None)
    await state.set_state(GenerationStates.choosing_settings)
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.callback_query(lambda cb: cb.data == SETTINGS_CONTINUE)
async def continue_after_settings(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Перейти от настроек к следующему шагу ввода по типу модели."""
    log_generation_callback(callback)
    state_data = await state.get_data()
    lang = get_state_language(state_data, callback.from_user) if state_data.get("user_language") else await get_event_lang(callback, session)
    await state.update_data(user_language=lang)
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    if not model:
        if state_data.get("model_generation_type") == "lipsync":
            await state.set_state(GenerationStates.waiting_for_image)
            await prompt_for_generation_input(callback.message, edit=True, is_lipsync=True, lang=lang)
            await callback.answer()
            return
        await callback.message.edit_text(
            format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang),
            reply_markup=None,
        )
        await callback.answer()
        return

    required_input_type = get_model_required_input_type(model)
    if required_input_type == "text":
        await set_waiting_for_prompt_with_diagnostic(
            state,
            user_id=callback.from_user.id,
            incoming_text_type=get_incoming_text_type(is_callback=True),
        )
        await callback.message.edit_text(get_prompt_for_generation_type(model.generation_type, lang), reply_markup=None)
        await callback.message.answer(
            t("generation.back_to_settings_hint", lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
    elif required_input_type == "image":
        if model.supports_multiple_images and model.input_media_field == "images":
            existing_urls = get_input_media_urls(state_data)
            await state.set_state(GenerationStates.waiting_for_images)
            if existing_urls:
                progress_text = t(
                    "generation.images_uploaded_progress",
                    lang,
                    count=len(existing_urls),
                    max_count=model.max_images,
                )
                await callback.message.edit_text(progress_text, reply_markup=None)
                await callback.message.answer(
                    progress_text,
                    reply_markup=build_media_upload_reply_keyboard(
                        show_continue=True,
                        lang=lang,
                        show_clear_images=True,
                    ),
                )
            else:
                await prompt_for_generation_images(callback.message, edit=True, model=model, lang=lang)
        else:
            await state.set_state(GenerationStates.waiting_for_image)
            await prompt_for_generation_image(callback.message, edit=True, model=model, lang=lang)
    elif required_input_type == "video":
        await state.set_state(GenerationStates.waiting_for_video)
        await prompt_for_generation_video(callback.message, edit=True, model=model, lang=lang)
    else:
        await state.set_state(GenerationStates.waiting_for_image)
        await prompt_for_generation_input(callback.message, edit=True, is_lipsync=True, lang=lang)
    await callback.answer()


async def navigate_back_to_settings(message: Message, state: FSMContext) -> None:
    """Вернуть пользователя к настройкам модели или к выбору раздела генерации."""
    state_data = await state.get_data()
    from_state = await state.get_state()
    selected_model_key = state_data.get("selected_model_key") or state_data.get("model_key")
    selected_settings = dict(state_data.get("selected_settings") or state_data.get("user_settings") or {})

    log_generation_diagnostic(
        action="back_to_settings",
        user_id=message.from_user.id,
        state_value=from_state,
        state_data=state_data,
        incoming_text_type=get_incoming_text_type(message),
    )

    await state.update_data(
        prompt=None,
        input_audio_or_text=None,
        selected_model_key=selected_model_key,
        selected_settings=selected_settings,
    )

    model = None
    if selected_model_key:
        try:
            model = get_generation_model(str(selected_model_key))
        except ValueError:
            model = None

    if model is None:
        await reset_generation_state(state)
        await state.set_state(GenerationStates.choosing_generation_type)
        await state.update_data(selected_generation_type=None, selected_provider=None)
        lang = get_state_language(state_data, message.from_user)
        await message.answer(t("generation.back_to_sections", lang), reply_markup=get_main_menu_keyboard(lang))
        await render_models_screen(message, lang)
        return

    await state.update_data(
        model_key=model.key,
        model_title=model.title,
        model_endpoint=model.endpoint,
        model_generation_type=model.generation_type,
        user_settings=selected_settings,
    )
    await state.set_state(GenerationStates.choosing_settings)
    lang = get_state_language(state_data, message.from_user)
    await message.answer(t("generation.back_to_model_settings", lang), reply_markup=get_main_menu_keyboard(lang))
    await message.answer(
        build_settings_text(model, selected_settings, lang),
        reply_markup=build_model_settings_keyboard(model, selected_settings, lang),
        parse_mode="HTML",
    )


@router.message(
    StateFilter(
        GenerationStates.waiting_for_image,
        GenerationStates.waiting_for_images,
        GenerationStates.waiting_for_video,
        GenerationStates.waiting_for_audio,
        GenerationStates.waiting_for_prompt,
    ),
    lambda message: is_localized_button_text(message.text, "common.back_to_settings", getattr(message.from_user, "language_code", None)),
)
async def back_to_settings_from_input_step(message: Message, state: FSMContext):
    """Вернуться с этапа ввода к настройкам модели без запуска генерации."""
    await navigate_back_to_settings(message, state)


@router.message(GenerationStates.waiting_for_image, lambda message: bool(message.photo) or bool(message.document) or bool(message.video))
async def process_generation_image(message: Message, state: FSMContext, *, from_media_group: bool = False):
    """Принять изображение для генерации."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    is_lipsync = is_lipsync_generation_state(state_data)
    model_generation_type = str(state_data.get("model_generation_type") or "image_edit")
    if get_media_group_key(message) and not from_media_group and not is_lipsync:
        await add_media_group_message(message, state, mode="single_image")
        return
    if is_lipsync:
        document = message.document
        photo = message.photo[-1] if message.photo else None
        video = message.video

        if document and not is_supported_media_document(document, is_lipsync=True):
            await message.answer(
                t("generation.lipsync_need_face_media", lang),
                reply_markup=build_back_to_settings_keyboard(lang),
            )
            return

        if not any([document, photo, video]):
            await invalid_generation_image(message, state)
            return

        input_media = build_input_media_payload(message)
        input_image_file_id = None
        if input_media.get("type") in {"photo", "image"}:
            input_image_file_id = input_media.get("file_id")

        await state.update_data(input_media=input_media, input_image_file_id=input_image_file_id)
        await set_waiting_for_prompt_with_diagnostic(
            state,
            user_id=message.from_user.id,
            incoming_text_type=get_incoming_text_type(message),
        )
        await message.answer(
            get_second_step_prompt_text(is_lipsync=True, lang=lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    received_type = None
    if message.video or (message.document and extract_document_media_type(message.document) == "video"):
        received_type = "video"
    if not is_supported_image_input(message):
        await message.answer(
            build_invalid_input_message("image", model_generation_type, received_type=received_type, lang=lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    try:
        media_item = await upload_message_media_item(message)
    except ImageUploadError:
        log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=message.from_user.id, status="failed")
        await message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, t("errors.prepare_image_failed", lang), lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    await state.update_data(
        input_media={"type": media_item["type"], "count": 1},
        input_media_items=[media_item],
        input_media_urls=[media_item["public_url"]],
        input_media_paths=[media_item["local_path"]],
        input_media_file_ids=[media_item.get("file_id", "")],
        input_image_file_id=media_item.get("file_id"),
        input_video_url=media_item["public_url"],
    )
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    if model and model_requires_audio_file(model):
        await state.set_state(GenerationStates.waiting_for_audio)
        await message.answer(
            t("generation.send_audio_for_lipsync", lang) if model.generation_type == "lipsync" else t("generation.send_audio", lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return
    if model and await show_confirmation_if_media_completes_model(message, state, model):
        return
    await set_waiting_for_prompt_with_diagnostic(
        state,
        user_id=message.from_user.id,
        incoming_text_type=get_incoming_text_type(message),
    )
    await message.answer(
        get_second_prompt_for_generation_type(model_generation_type, lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


@router.message(GenerationStates.waiting_for_images, lambda message: bool(message.photo) or bool(message.document) or bool(message.video))
async def process_generation_images(message: Message, state: FSMContext):
    """Принять очередное изображение для multi-image модели."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    model_generation_type = str(state_data.get("model_generation_type") or "image_edit")
    received_type = None
    if message.video or (message.document and extract_document_media_type(message.document) == "video"):
        received_type = "video"
    if not model:
        await message.answer(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang))
        return
    if get_media_group_key(message):
        await add_media_group_message(message, state, mode="multi_image")
        return
    if not is_supported_image_input(message):
        await message.answer(
            build_invalid_input_message("image", model_generation_type, received_type=received_type, lang=lang),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
        )
        return

    current_urls = get_input_media_urls(state_data)
    if len(current_urls) >= model.max_images:
        await message.answer(
            t("generation.image_limit_reached", lang, count=model.max_images),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang, show_clear_images=True),
        )
        return

    try:
        media_item = await upload_message_media_item(message)
    except ImageUploadError:
        log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=message.from_user.id, status="failed")
        await message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, t("errors.prepare_image_failed", lang), lang),
            reply_markup=build_media_upload_reply_keyboard(
                show_continue=True,
                lang=lang,
                show_clear_images=bool(current_urls),
            ),
        )
        return

    current_paths = get_input_media_paths(state_data)
    current_file_ids = get_input_media_file_ids(state_data)
    updated_urls = [*current_urls, media_item["public_url"]]
    updated_paths = [*current_paths, media_item["local_path"]]
    updated_file_ids = [*current_file_ids, media_item.get("file_id", "")]
    await state.update_data(
        input_media_items=build_input_media_items_from_lists(updated_urls, updated_paths, updated_file_ids),
        input_media_urls=updated_urls,
        input_media_paths=updated_paths,
        input_media_file_ids=updated_file_ids,
        input_media={"type": "images", "count": len(updated_urls)},
        input_image_file_id=updated_file_ids[0] if updated_file_ids else None,
    )
    if len(updated_urls) >= model.max_images:
        await set_waiting_for_prompt_with_diagnostic(
            state,
            user_id=message.from_user.id,
            incoming_text_type=get_incoming_text_type(message),
        )
        await message.answer(
            get_second_prompt_for_generation_type(model_generation_type, lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    await message.answer(
        t("generation.images_uploaded_progress", lang, count=len(updated_urls), max_count=model.max_images),
        reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang, show_clear_images=True),
    )


@router.message(GenerationStates.waiting_for_images, lambda message: is_localized_button_text(message.text, "common.clear_images", getattr(message.from_user, "language_code", None)))
async def clear_uploaded_images(message: Message, state: FSMContext):
    """Очистить загруженные изображения multi-image flow без сброса настроек модели."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    await cleanup_state_media(state)
    await state.set_state(GenerationStates.waiting_for_images)
    await message.answer(
        t("generation.images_cleared", lang),
        reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
    )


@router.message(GenerationStates.waiting_for_image)
async def invalid_generation_image(message: Message, state: FSMContext):
    """Сообщить, что ожидается изображение."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    is_lipsync = is_lipsync_generation_state(state_data)
    await message.answer(
        t("generation.invalid_wait_lipsync", lang) if is_lipsync else t("generation.waiting_for_image_error", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


@router.message(GenerationStates.waiting_for_images)
async def invalid_generation_images(message: Message, state: FSMContext):
    """Сообщить, что ожидается изображение для multi-image flow."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    max_images = model.max_images if model else 1
    await message.answer(
        format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, t("generation.flow.image.invalid", lang), lang),
        reply_markup=build_media_upload_reply_keyboard(show_continue=True if max_images else False, lang=lang),
    )


@router.message(GenerationStates.waiting_for_video, lambda message: bool(message.photo) or bool(message.document) or bool(message.video))
async def process_generation_video(message: Message, state: FSMContext):
    """Принять видео для генерации."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    model_generation_type = str(state_data.get("model_generation_type") or "video_edit")
    received_type = None
    if message.photo or (message.document and extract_document_media_type(message.document) == "image"):
        received_type = "image"

    if not is_supported_video_input(message):
        await message.answer(
            build_invalid_input_message("video", model_generation_type, received_type=received_type, lang=lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    try:
        media_item = await upload_message_media_item(message)
    except ImageUploadError:
        log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=message.from_user.id, status="failed")
        await message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, t("errors.prepare_video_failed", lang), lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    await state.update_data(
        input_media={"type": media_item["type"], "count": 1},
        input_media_items=[media_item],
        input_media_urls=[media_item["public_url"]],
        input_media_paths=[media_item["local_path"]],
        input_media_file_ids=[media_item.get("file_id", "")],
        input_image_file_id=media_item.get("file_id"),
    )
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    if model and model_requires_audio_file(model):
        await state.set_state(GenerationStates.waiting_for_audio)
        await message.answer(
            t("generation.send_audio_for_lipsync", lang) if model.generation_type == "lipsync" else t("generation.send_audio", lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return
    await set_waiting_for_prompt_with_diagnostic(
        state,
        user_id=message.from_user.id,
        incoming_text_type=get_incoming_text_type(message),
    )
    await message.answer(
        get_second_prompt_for_generation_type(model_generation_type, lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


@router.message(GenerationStates.waiting_for_video)
async def invalid_generation_video(message: Message, state: FSMContext):
    """Сообщить, что ожидается видео."""
    state_data = await state.get_data()
    model_generation_type = str(state_data.get("model_generation_type") or "video_edit")
    lang = get_state_language(state_data, message.from_user)
    await message.answer(
            t("generation.waiting_for_video_error", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


@router.message(GenerationStates.waiting_for_audio, lambda message: bool(message.voice) or bool(message.audio) or bool(message.document))
async def process_generation_audio(message: Message, state: FSMContext, session: AsyncSession):
    """Принять аудио для моделей с отдельным audio-входом."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    if not model:
        await message.answer(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang))
        return
    if not is_supported_audio_input(message):
        await message.answer(
            build_user_error_message("generation.unsupported_audio_type", lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    max_size_bytes = get_audio_max_size_bytes(model)
    file_size = get_audio_input_file_size(message)
    if max_size_bytes is not None and file_size is not None and file_size > max_size_bytes:
        await message.answer(
            build_user_error_message("generation.audio_too_large", lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    audio_payload = build_input_audio_or_text_payload(message)
    audio_file_id = audio_payload.get("file_id")
    if not audio_file_id:
        await message.answer(
            build_user_error_message("generation.unsupported_audio_type", lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    try:
        telegram_files = TelegramFilesService(message.bot)
        temp_audio = await telegram_files.download_temp_file_and_get_public_url(str(audio_file_id))
    except ImageUploadError:
        log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=message.from_user.id, status="failed")
        await message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, t("errors.prepare_media_failed", lang), lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return

    await state.update_data(
        input_audio_url=temp_audio.public_url,
        input_audio_path=str(temp_audio.local_path),
        input_audio_file_id=str(audio_file_id),
        input_audio_or_text={**audio_payload, "public_url": temp_audio.public_url},
        prompt=t("generation.audio_file", lang),
    )
    if model_requires_prompt_input(model):
        await set_waiting_for_prompt_with_diagnostic(
            state,
            user_id=message.from_user.id,
            incoming_text_type=get_incoming_text_type(message),
        )
        await message.answer(
            t("generation.audio_received", lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        await message.answer(
            get_second_prompt_for_generation_type(model.generation_type, lang),
            reply_markup=build_back_to_settings_keyboard(lang),
        )
        return
    await message.answer(t("generation.audio_received", lang))
    await send_confirmation_screen(
        message=message,
        state=state,
        session=session,
        telegram_user=message.from_user,
        edit=False,
    )


@router.message(GenerationStates.waiting_for_audio)
async def invalid_generation_audio(message: Message, state: FSMContext):
    """Сообщить, что ожидается аудиофайл."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    await message.answer(
        build_user_error_message("generation.waiting_for_audio_error", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


@router.message(GenerationStates.waiting_for_prompt)
async def process_prompt(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
):
    """Обработать промпт для генерации."""
    try:
        state_data = await state.get_data()
        lang = get_state_language(state_data, message.from_user)
        model_key = state_data.get("model_key", "nano_banana")
        model = get_generation_model(model_key)
        is_lipsync = is_lipsync_generation_state(state_data)
        required_input_type = get_model_required_input_type(model)
        input_media = state_data.get("input_media")
        input_media_items = get_input_media_items(state_data)
        input_audio_or_text = None
        prompt = ""
        log_generation_diagnostic(
            action="process_prompt",
            user_id=message.from_user.id,
            state_value=await state.get_state(),
            state_data=state_data,
            incoming_text_type=get_incoming_text_type(message),
            prompt=(message.text or "").strip(),
        )
        
        if is_lipsync:
            input_audio_or_text = build_input_audio_or_text_payload(message)
            prompt = get_input_audio_or_text_display(input_audio_or_text, lang)
            if not input_audio_or_text:
                message_key = "generation.send_audio_for_lipsync" if model.requires_audio else "generation.lipsync_need_text"
                await message.answer(t(message_key, lang) if message_key == "generation.send_audio_for_lipsync" else build_user_error_message(message_key, lang))
                return
            if model.requires_audio and not is_audio_input_payload(input_audio_or_text):
                await message.answer(t("generation.send_audio_for_lipsync", lang))
                return
            if model.requires_prompt and not is_text_input_payload(input_audio_or_text):
                await message.answer(format_user_error(ErrorCode.E002_MISSING_PROMPT, get_flow_texts(model.generation_type, lang).missing_prompt, lang))
                return
            if not state_has_required_media(model, input_media, input_media_items):
                await message.answer(
                    format_user_error(ErrorCode.E003_MISSING_IMAGE, t("generation.lipsync_need_media", lang), lang),
                    reply_markup=build_back_to_settings_keyboard(lang),
                )
                await state.set_state(GenerationStates.waiting_for_video if model.input_media_field == "video" else GenerationStates.waiting_for_image)
                return
        else:
            if message_contains_file(message):
                await message.answer(
                    build_invalid_input_message("text", model.generation_type, lang=lang),
                    reply_markup=build_back_to_settings_keyboard(lang),
                )
                return
            prompt = (message.text or "").strip()
            if not prompt:
                await message.answer(
                    format_user_error(ErrorCode.E002_MISSING_PROMPT, get_flow_texts(model.generation_type, lang).missing_prompt, lang),
                    reply_markup=build_back_to_settings_keyboard(lang),
                )
                return
            if len(prompt) < 10:
                await message.answer(
                    format_user_error(ErrorCode.E002_MISSING_PROMPT, t("errors.short_description", lang), lang),
                    reply_markup=build_back_to_settings_keyboard(lang),
                )
                return
            if model.input_media_field == "images" and len(input_media_items) < model.min_images and not input_media:
                await message.answer(
                    format_user_error(ErrorCode.E003_MISSING_IMAGE, t("generation.need_min_images", lang, count=model.min_images), lang),
                    reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
                )
                await state.set_state(GenerationStates.waiting_for_images)
                return
            if required_input_type == "image" and not input_media:
                await message.answer(
                    format_user_error(ErrorCode.E003_MISSING_IMAGE, get_flow_texts(model.generation_type, lang).missing_media, lang),
                    reply_markup=build_back_to_settings_keyboard(lang),
                )
                await state.set_state(GenerationStates.waiting_for_image)
                return
            if required_input_type == "video" and not input_media:
                await message.answer(
                    format_user_error(ErrorCode.E004_MISSING_VIDEO, get_flow_texts(model.generation_type, lang).missing_media, lang),
                    reply_markup=build_back_to_settings_keyboard(lang),
                )
                await state.set_state(GenerationStates.waiting_for_video)
                return
        
        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        
        if not user:
            await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.user_not_found", lang), lang))
            return
        lang = get_user_preferred_language(user, message.from_user)
        
        user_settings = get_model_state_settings(state_data, model_key)
        total_cost = get_total_generation_cost(model, user_settings)
        if not await user_repo.has_enough_balance(user.id, total_cost):
            log_generation_diagnostic(
                action="insufficient_balance",
                user_id=user.id,
                state_value=await state.get_state(),
                state_data=state_data,
                incoming_text_type=get_incoming_text_type(message),
                prompt=prompt,
                total_cost=total_cost,
            )
            await state.update_data(prompt=None, input_audio_or_text=None)
            await state.set_state(GenerationStates.choosing_settings)
            await answer_insufficient_balance(
                message,
                lang=lang,
                user_id=user.id,
                balance=user.balance,
                required_balance=total_cost,
                model_key=model_key,
            )
            return

        await state.update_data(prompt=prompt, input_audio_or_text=input_audio_or_text)
        await send_confirmation_screen(
            message=message,
            state=state,
            session=session,
            telegram_user=message.from_user,
            edit=False,
        )
    except Exception:
        logger.exception("Error in process_prompt")
        log_generation_error(
            ErrorCode.E010_INTERNAL_ERROR,
            user_id=message.from_user.id,
            model_key=state_data.get("model_key"),
            status="failed",
        )
        await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.request_processing_failed", lang), lang))


@router.callback_query(lambda cb: cb.data == GENERATION_CONFIRM)
async def confirm_generation(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Запустить генерацию после подтверждения."""
    log_generation_callback(callback)
    user_id = callback.from_user.id
    state_data = await state.get_data()
    lang = get_state_language(state_data, callback.from_user)

    model_key = state_data.get("model_key")
    model_title = state_data.get("model_title")
    model_endpoint = state_data.get("model_endpoint")
    prompt = str(state_data.get("prompt") or "")
    input_media = state_data.get("input_media")
    input_media_items = get_input_media_items(state_data)
    input_image_file_id = state_data.get("input_image_file_id")
    is_lipsync = is_lipsync_generation_state(state_data)
    model = get_generation_model(model_key) if model_key else None
    required_input_type = get_model_required_input_type(model) if model else "text"
    log_generation_diagnostic(
        action="confirm_generation",
        user_id=user_id,
        state_value=await state.get_state(),
        state_data=state_data,
        incoming_text_type=get_incoming_text_type(is_callback=True),
    )
    if not input_media and input_image_file_id:
        legacy_media_type = "image" if required_input_type != "video" else "video"
        input_media = {"type": legacy_media_type, "file_id": input_image_file_id}
    try:
        user_settings = validate_model_settings(model_key, state_data.get("user_settings")) if model_key else {}
    except ValueError:
        log_generation_error(ErrorCode.E011_INVALID_MODEL_SETTINGS, user_id=user_id, model_key=model_key, status="rejected")
        await callback.message.answer(
            format_user_error(ErrorCode.E011_INVALID_MODEL_SETTINGS, t("errors.invalid_model_settings", lang), lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
        await reset_generation_state(state)
        await callback.answer()
        return

    input_audio_or_text = state_data.get("input_audio_or_text")
    is_complete = bool(
        model_key
        and model_title
        and model_endpoint
        and model
        and state_has_required_prompt_or_audio(model, str(prompt or ""), input_audio_or_text)
        and state_has_required_media(model, input_media, input_media_items)
    )

    if not is_complete:
        if not model:
            error_text = format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang)
        elif is_lipsync:
            error_text = get_lipsync_incomplete_error_text(lang)
        elif not prompt:
            error_text = format_user_error(ErrorCode.E002_MISSING_PROMPT, get_flow_texts(model.generation_type, lang).missing_prompt, lang)
        elif model and model.input_media_field == "images":
            error_text = format_user_error(ErrorCode.E003_MISSING_IMAGE, t("generation.need_min_images", lang, count=model.min_images), lang)
        elif required_input_type == "image":
            error_text = format_user_error(ErrorCode.E003_MISSING_IMAGE, get_flow_texts(model.generation_type, lang).missing_media, lang)
        elif required_input_type == "video":
            error_text = format_user_error(ErrorCode.E004_MISSING_VIDEO, get_flow_texts(model.generation_type, lang).missing_media, lang)
        else:
            error_text = format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.incomplete_generation", lang), lang)
        await callback.message.answer(
            error_text,
            reply_markup=get_main_menu_keyboard(lang),
        )
        await reset_generation_state(state)
        await callback.answer()
        return

    debited_balance = False
    debited_user_id: Optional[int] = None
    generation_request_id = None
    generation_request_ids: list[Any] = []
    generation_predictions: list[tuple[Any, str]] = []
    temp_input_path: Optional[str] | list[str] = None
    task_started = False
    total_cost = 0
    single_generation_cost = 0

    try:
        user_repo = UserRepository(session)
        generation_repo = GenerationRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
        lang = get_user_preferred_language(user, callback.from_user)
        await state.update_data(user_language=lang)
        debited_user_id = user.id
        num_generations = get_model_num_generations(model, user_settings)
        total_cost = get_total_generation_cost(model, user_settings)
        allocated_generation_costs = allocate_generation_cost_credits(model, user_settings, num_generations)
        single_generation_cost = allocated_generation_costs[0]

        if user.balance < total_cost:
            log_generation_diagnostic(
                action="insufficient_balance",
                user_id=user.id,
                state_value=await state.get_state(),
                state_data=state_data,
                incoming_text_type=get_incoming_text_type(is_callback=True),
                prompt=str(prompt or ""),
                total_cost=total_cost,
            )
            await answer_insufficient_balance(
                callback.message,
                lang=lang,
                user_id=user.id,
                balance=user.balance,
                required_balance=total_cost,
                model_key=model_key,
            )
            await reset_generation_state(state)
            await callback.answer()
            return

        media_urls: list[str] = []
        temp_input_paths: list[str] = []
        if input_media_items:
            media_urls = [item["public_url"] for item in input_media_items if item.get("public_url")]
            temp_input_paths.extend(item["local_path"] for item in input_media_items if item.get("local_path"))
        elif model and model_requires_media(model) and input_media:
            telegram_files = TelegramFilesService(callback.bot)
            temp_media = await telegram_files.download_temp_file_and_get_public_url(str(input_media.get("file_id")))
            media_urls = [temp_media.public_url]
            temp_input_paths.append(str(temp_media.local_path))

        payload_user_settings = dict(user_settings)
        if model and model.input_media_field == "video":
            input_video_url = state_data.get("input_video_url") or (media_urls[0] if media_urls else None)
            if isinstance(input_video_url, str) and input_video_url:
                payload_user_settings["input_video_url"] = input_video_url
        if model and model.requires_audio:
            audio_url = state_data.get("input_audio_url")
            audio_path = state_data.get("input_audio_path")
            if isinstance(audio_url, str) and audio_url:
                payload_user_settings["input_audio_url"] = audio_url
                if isinstance(audio_path, str) and audio_path:
                    temp_input_paths.append(audio_path)
            else:
                audio_input = input_audio_or_text if isinstance(input_audio_or_text, dict) else {}
                audio_file_id = audio_input.get("file_id") if audio_input.get("type") in {"voice", "audio"} else None
                if audio_file_id:
                    telegram_files = TelegramFilesService(callback.bot)
                    temp_audio = await telegram_files.download_temp_file_and_get_public_url(str(audio_file_id))
                    payload_user_settings["input_audio_url"] = temp_audio.public_url
                    temp_input_paths.append(str(temp_audio.local_path))
        if len(temp_input_paths) == 1:
            temp_input_path = temp_input_paths[0]
        else:
            temp_input_path = temp_input_paths or None
        payload = build_payload(model_key, media_urls, prompt, payload_user_settings)

        if not await user_repo.decrease_balance(user.id, total_cost):
            log_generation_diagnostic(
                action="insufficient_balance",
                user_id=user.id,
                state_value=await state.get_state(),
                state_data=state_data,
                incoming_text_type=get_incoming_text_type(is_callback=True),
                prompt=str(prompt or ""),
                total_cost=total_cost,
            )
            await answer_insufficient_balance(
                callback.message,
                lang=lang,
                user_id=user.id,
                balance=user.balance,
                required_balance=total_cost,
                model_key=model_key,
            )
            await reset_generation_state(state)
            await callback.answer()
            return
        debited_balance = True
        log_balance_event("balance_debited", user.id, total_cost)

        generation_costs_by_id: dict[Any, int] = {}
        for generation_cost in allocated_generation_costs:
            generation_request = await generation_repo.create_generation_request(
                user_id=user.id,
                chat_id=callback.message.chat.id,
                model_key=model_key,
                model_endpoint=model_endpoint,
                prompt=prompt,
                settings=user_settings,
                input_image_file_ids=[],
                input_image_urls=[],
                aspect_ratio=user_settings.get("aspect_ratio"),
                resolution=user_settings.get("resolution"),
                size=user_settings.get("size"),
                output_format=user_settings.get("output_format"),
                status="created",
                cost=generation_cost,
            )
            generation_request_ids.append(generation_request.id)
            generation_costs_by_id[generation_request.id] = generation_cost

        generation_request_id = generation_request_ids[0] if generation_request_ids else None
        for current_generation_request_id in generation_request_ids:
            prediction_id = await submit_generation_request(
                generation_request_id=current_generation_request_id,
                user_id=user_id,
                model_key=model_key,
                payload=dict(payload),
            )
            generation_predictions.append((current_generation_request_id, prediction_id))

        if num_generations == 1:
            prediction_id = generation_predictions[0][1]
            log_background_generation_event(
                "background_task_scheduled",
                generation_id=generation_request_id,
                user_id=user_id,
                chat_id=callback.message.chat.id,
                model_key=model_key,
                prediction_id=prediction_id,
            )
            task = asyncio.create_task(
                poll_generation_result(
                    bot=callback.bot,
                    user_id=user_id,
                    chat_id=callback.message.chat.id,
                    generation_request_id=generation_request_id,
                    prediction_id=prediction_id,
                    model_key=model_key,
                    cost=generation_costs_by_id.get(generation_request_id, single_generation_cost),
                    temp_input_path=temp_input_path,
                )
            )
        else:
            for current_generation_request_id, prediction_id in generation_predictions:
                log_background_generation_event(
                    "background_task_scheduled",
                    generation_id=current_generation_request_id,
                    user_id=user_id,
                    chat_id=callback.message.chat.id,
                    model_key=model_key,
                    prediction_id=prediction_id,
                )
            task = asyncio.create_task(
                poll_generation_results_batch(
                    bot=callback.bot,
                    user_id=user_id,
                    chat_id=callback.message.chat.id,
                    generation_predictions=generation_predictions,
                    model_key=model_key,
                    cost=single_generation_cost,
                    generation_costs=generation_costs_by_id,
                    temp_input_path=temp_input_path,
                )
            )
        task.add_done_callback(log_background_task_exception)
        task_started = True
        for current_generation_request_id in generation_request_ids:
            BACKGROUND_GENERATIONS[current_generation_request_id] = {
                "task": task,
                "generation_request_id": current_generation_request_id,
                "generation_request_ids": generation_request_ids,
            }
        await state.clear()

        await callback.message.edit_text(
            t("generation.started_count", lang, model=escape(model_title), count=num_generations),
            parse_mode="HTML",
        )
        await callback.message.answer(
            t("generation.started_background", lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
        await callback.answer()
    except ImageUploadError as exc:
        logger.exception("Media upload failed before generation start")
        log_generation_error(
            ErrorCode.E012_MEDIA_UPLOAD_FAILED,
            generation_id=generation_request_id,
            user_id=debited_user_id,
            model_key=model_key,
            status="failed",
        )
        if generation_request_ids:
            generation_repo = GenerationRepository(session)
            for current_generation_request_id in generation_request_ids:
                await generation_repo.update_generation_status(
                    current_generation_request_id,
                    "failed",
                    error_message=exc.user_message,
                )
        if debited_balance and debited_user_id is not None:
            try:
                user_repo = UserRepository(session)
                await user_repo.increase_balance(debited_user_id, total_cost)
            except Exception as refund_exc:
                logger.exception("Error while refunding balance after image upload failure: %s", refund_exc)
        await state.update_data(
            input_image_file_id=None,
            input_media=None,
            input_media_items=[],
            input_media_urls=[],
            input_media_paths=[],
            input_media_file_ids=[],
        )
        waiting_state = GenerationStates.waiting_for_images if model and model.supports_multiple_images else get_waiting_state_for_input_type(required_input_type)
        await state.set_state(waiting_state)
        await callback.message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, t("errors.prepare_media_failed", lang), lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
        await callback.answer()
    except ValueError:
        log_generation_error(
            ErrorCode.E011_INVALID_MODEL_SETTINGS,
            generation_id=generation_request_id,
            user_id=debited_user_id,
            model_key=model_key,
            status="failed",
        )
        if debited_balance and debited_user_id is not None:
            try:
                user_repo = UserRepository(session)
                await user_repo.increase_balance(debited_user_id, total_cost)
            except Exception:
                logger.exception("Error while refunding balance after invalid payload")
        await state.clear()
        if str(exc) == "missing_docs_contract":
            message_text = format_user_error(
                ErrorCode.E011_INVALID_MODEL_SETTINGS,
                t("errors.model_contract_unavailable", lang),
                lang,
            )
        else:
            message_text = format_user_error(ErrorCode.E011_INVALID_MODEL_SETTINGS, t("errors.invalid_model_settings", lang), lang)
        await callback.message.answer(
            message_text,
            reply_markup=get_main_menu_keyboard(lang),
        )
        await callback.answer()
    except Exception:
        logger.exception("Error while launching generation")
        log_generation_error(
            ErrorCode.E010_INTERNAL_ERROR,
            generation_id=generation_request_id,
            user_id=debited_user_id,
            model_key=model_key,
            status="failed",
        )
        if generation_request_ids:
            generation_repo = GenerationRepository(session)
            for current_generation_request_id in generation_request_ids:
                await generation_repo.update_generation_status(
                    current_generation_request_id,
                    "failed",
                    error_message=t("errors.finish_generation_failed", lang),
                )
        if debited_balance and debited_user_id is not None:
            try:
                user_repo = UserRepository(session)
                await user_repo.increase_balance(debited_user_id, total_cost)
            except Exception as refund_exc:
                logger.exception("Error while refunding balance after launch failure: %s", refund_exc)
        if not task_started:
            await cleanup_generation_file(temp_input_path)
        await state.clear()
        await callback.message.answer(
            format_user_error(ErrorCode.E010_INTERNAL_ERROR, t("errors.launch_generation_failed", lang), lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
        await callback.answer()
@router.callback_query(F.data.startswith("gen:"))
async def handle_unknown_generation_callback(callback: CallbackQuery, state: FSMContext, session: Optional[AsyncSession] = None):
    """Fallback для устаревших или неподдерживаемых inline-кнопок генераций."""
    log_generation_callback(callback)
    lang = await get_event_lang(callback, session)
    await state.set_state(GenerationStates.choosing_generation_type)
    await state.update_data(selected_generation_type=None, selected_provider=None, user_language=lang)
    await callback.answer(build_user_error_message("generation.legacy_button", lang), show_alert=True)
    await callback.message.edit_text(
        build_generation_types_screen_text(lang),
        reply_markup=build_generation_sections_keyboard(lang),
        parse_mode="HTML",
    )
