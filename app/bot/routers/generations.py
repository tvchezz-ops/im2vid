"""Роутер генерации контента."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html import escape
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, Optional
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
    build_media_upload_reply_keyboard,
    build_generation_confirm_keyboard,
    build_generation_sections_keyboard,
    build_generation_type_keyboard,
    build_models_keyboard,
    build_model_selection_keyboard,
    build_model_settings_keyboard,
    build_providers_keyboard,
    build_provider_keyboard,
    build_setting_options_keyboard,
    get_main_menu_keyboard,
    is_localized_button_text,
    resolve_model_key_from_token,
)
from app.bot.states import GenerationStates
from app.config import settings
from app.db import GenerationRepository, GenerationRequestStatus, UserRepository
from app.db.session import db_manager
from app.i18n import get_user_language, t
from app.services.generation_service import (
    GenerationModel,
    build_payload,
    get_default_settings,
    get_generation_model,
    get_model_num_generations,
    get_required_input_type,
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
    if user is not None:
        return get_user_language(getattr(user, "language_code", None))
    return get_actor_language(actor)


def get_state_language(state_data: dict[str, Any], actor: Any | None = None, user: Any | None = None) -> str:
    if user is not None:
        return get_user_preferred_language(user)
    if state_data.get("user_language"):
        return get_user_language(str(state_data.get("user_language")))
    return get_actor_language(actor)

BACKGROUND_GENERATIONS: Dict[Any, Dict[str, Any]] = {}
GENERATION_COST = 1
DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS = 3600
DOCUMENT_SEND_RETRY_COUNT = 3
OUTPUT_DOWNLOAD_TIMEOUT_SECONDS = 300

GENERATION_FLOW_STATE_NAMES = {
    GenerationStates.choosing_generation_type.state,
    GenerationStates.choosing_provider.state,
    GenerationStates.choosing_settings.state,
    GenerationStates.choosing_setting_value.state,
    GenerationStates.waiting_for_setting_text.state,
    GenerationStates.waiting_for_images.state,
    GenerationStates.waiting_for_image.state,
    GenerationStates.waiting_for_video.state,
    GenerationStates.waiting_for_prompt.state,
    GenerationStates.waiting_for_confirmation.state,
}

MODEL_PREFIX = "gen:model:"
GENERATION_SECTION_PREFIX = "gen:section:"
GENERATION_ALL = "gen:all"
PROVIDER_PREFIX = "gen:provider:"
SETTINGS_OPEN_PREFIX = "gen:setting:"
SETTINGS_VALUE_PREFIX = "gen:set:"
BACK_TO_SECTIONS = "gen:back:sections"
BACK_TO_PROVIDERS = "gen:back:providers"
SETTINGS_BACK_PREFIX = "gen:back:settings"
SETTINGS_BACK_MODELS = "gen:back:models"
SETTINGS_CONTINUE = "gen:continue"
GENERATION_CONFIRM = "gen:confirm"


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


async def count_active_generations_for_user(user_id: int, session: Optional[AsyncSession] = None) -> int:
    if session is not None:
        return await GenerationRepository(session).count_active_generations(user_id)
    async with db_manager.session_factory() as new_session:
        return await GenerationRepository(new_session).count_active_generations(user_id)


def log_parallel_generation_event(action: str, user_id: int, count: int, limit: int) -> None:
    logger.info(
        {
            "action": action,
            "user_id": user_id,
            "active_count": count,
            "limit": limit,
        }
    )


def format_user_error(code: str, message: str, lang: str = "en") -> str:
    return t("errors.formatted", lang, code=code, message=message)


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


GENERATION_TYPE_LABELS = {
    "text_to_image": "🖼 Text → Image",
    "text_to_video": "🎬 Text → Video",
    "image_edit": "🧩 Image Edit",
    "image_to_video": "🎥 Image → Video",
    "video_edit": "🎞 Video Edit",
    "lipsync": "🗣 Lipsync",
    "all": "📚 All models",
}

PROVIDER_LABELS = {
    "alibaba": "Alibaba",
    "openai": "OpenAI",
    "bytedance": "ByteDance",
    "google": "Google",
}


def get_generation_type_title(generation_type: str, lang: str) -> str:
    return t(f"generation.section_title.{generation_type}", lang)


def get_generation_type_description(generation_type: str, lang: str) -> str:
    return t(f"generation.section_details.{generation_type}", lang)


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
        f"- <b>{escape(setting.title)}</b>: <code>{escape(str(user_settings.get(setting.key, setting.default)))}</code>"
        for setting in model.user_settings.values()
    )


def get_total_generation_cost(model: GenerationModel, user_settings: dict[str, Any]) -> int:
    return GENERATION_COST * get_model_num_generations(model, user_settings)


def build_settings_text(model: GenerationModel, user_settings: dict[str, Any], lang: str = "en") -> str:
    """Собрать экран настроек модели."""
    if not model.user_settings:
        return (
            f"{t('generation.settings_header', lang, model=escape(model.title))}\n\n"
            f"{t('generation.settings_no_extra_full', lang)}"
        )
    return (
        f"{t('generation.settings_header', lang, model=escape(model.title))}\n\n"
        f"{t('generation.settings_choose', lang)}\n\n"
        f"{t('generation.settings_current', lang, values=format_generation_settings(model, user_settings))}"
    )


def build_setting_value_text(model: GenerationModel, setting_key: str, current_value: str, lang: str = "en") -> str:
    """Собрать экран выбора конкретной настройки."""
    setting = model.user_settings[setting_key]
    if setting.type == "text":
        description_block = f"\n\n{escape(setting.description)}" if setting.description else ""
        return (
            f"{t('generation.settings_header', lang, model=escape(model.title))}\n\n"
            f"{t('generation.setting_parameter', lang, parameter=escape(setting.title))}\n"
            f"{t('generation.setting_current_value', lang, value=escape(current_value))}\n\n"
            f"{t('generation.setting_send_text', lang, description=description_block)}"
        )
    options = "\n".join(f"• <code>{escape(option.value)}</code>" for option in setting.options)
    return (
        f"{t('generation.settings_header', lang, model=escape(model.title))}\n\n"
        f"{t('generation.setting_choose_value', lang)}\n\n"
        f"{t('generation.model_label', lang, model=escape(model.title))}\n"
        f"{t('generation.setting_parameter', lang, parameter=escape(setting.title))}\n"
        f"{t('generation.setting_current_value', lang, value=escape(current_value))}\n\n"
        f"{t('generation.setting_options', lang, options=options)}"
    )


def build_confirmation_text(
    model: GenerationModel,
    user_settings: dict[str, Any],
    prompt: str,
    balance: int,
    lang: str = "en",
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
        f"{t('generation.settings_label', lang, settings=format_generation_settings(model, user_settings))}\n\n"
        f"{prompt_line}\n\n"
        f"{t('generation.count_label', lang, count=num_generations)}\n"
        f"{t('generation.cost_label', lang, cost=total_cost)}\n"
        f"{t('generation.balance_after_label', lang, balance=balance_after_launch)}"
    )


def build_partial_generation_failed_message(lang: str = "en") -> str:
    return t("errors.formatted", lang, code=ErrorCode.E007_WAVESPEED_FAILED, message=t("errors.partial_generation_failed", lang))


def build_generated_but_delivery_failed_message(lang: str = "en") -> str:
    if lang == "ru":
        return "❌ Ошибка E010: результат был сгенерирован, но бот не смог его доставить. Кредит возвращён."
    return "❌ Error E010: the result was generated, but the bot could not deliver it. The credit was refunded."


def build_telegram_delivery_failed_refund_message(lang: str = "en") -> str:
    if lang == "ru":
        return "❌ Ошибка E009: файл готов, но Telegram не смог его доставить. Кредит возвращён."
    return "❌ Error E009: the file is ready, but Telegram could not deliver it. The credit was refunded."


def build_empty_outputs_failed_message(lang: str = "en") -> str:
    if lang == "ru":
        return "❌ Ошибка E010: провайдер завершил генерацию, но не вернул файл результата. Кредит возвращён."
    return "❌ Error E010: the provider completed the generation, but returned no result file. The credit was refunded."


def get_user_friendly_error_message(error: Exception, result: Optional[WavespeedResult] = None, lang: str = "en") -> str:
    """Вернуть безопасное и понятное сообщение об ошибке для пользователя."""
    if isinstance(error, WavespeedTimeoutError):
        return f"⏱ {t('errors.formatted', lang, code=ErrorCode.E008_WAVESPEED_TIMEOUT, message=t('errors.timeout_refund', lang)).removeprefix('❌ ')}"

    if isinstance(error, WavespeedFailedError) or (result is not None and result.status == "failed"):
        return (
            f"{t('errors.formatted', lang, code=ErrorCode.E007_WAVESPEED_FAILED, message=t('errors.generation_failed_refund', lang))}\n\n"
            f"{t('errors.rejected_by_provider', lang)}"
        )

    if isinstance(error, TelegramBadRequest):
        return t("errors.formatted", lang, code=ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, message=t("errors.telegram_delivery_failed", lang))

    if isinstance(error, (WavespeedNetworkError, aiohttp.ClientError, TimeoutError)):
        return t("errors.formatted", lang, code=ErrorCode.E010_INTERNAL_ERROR, message=t("errors.result_network_failure", lang))

    return t("errors.formatted", lang, code=ErrorCode.E010_INTERNAL_ERROR, message=t("errors.internal_retry", lang))


class OutputDeliveryTooLargeError(Exception):
    """Файл результата слишком большой для отправки в Telegram."""


def get_model_state_settings(state_data: dict[str, Any], model_key: str) -> dict[str, Any]:
    """Получить провалидированные настройки модели из FSM."""
    return validate_model_settings(model_key, state_data.get("user_settings"))


def is_lipsync_generation_state(state_data: dict[str, Any]) -> bool:
    """Определить, что текущий сценарий относится к lipsync."""
    return state_data.get("model_generation_type") == "lipsync"


def get_input_audio_or_text_display(value: Any, lang: str = "en") -> str:
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


def get_media_input_prompt_text(*, is_lipsync: bool, lang: str = "en") -> str:
    """Вернуть текст шага загрузки media-входа."""
    if is_lipsync:
        return get_flow_texts("lipsync", lang).initial_prompt
    return get_flow_texts("image_edit", lang).initial_prompt


def get_lipsync_incomplete_error_text(lang: str = "en") -> str:
    """Вернуть единое сообщение о неполных входных данных lipsync."""
    return f"❌ {t('generation.lipsync_incomplete', lang)}"


def get_second_step_prompt_text(*, is_lipsync: bool, lang: str = "en") -> str:
    """Вернуть текст второго шага после загрузки media."""
    if is_lipsync:
        return get_flow_texts("lipsync", lang).second_step_prompt
    return t("generation.second_step_text", lang)


def get_flow_texts(generation_type: str, lang: str = "en") -> FlowTexts:
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
        "lipsync": FlowTexts(
            initial_prompt=t("generation.flow.lipsync.initial", lang),
            second_step_prompt=t("generation.flow.lipsync.second", lang),
            missing_prompt=t("errors.e002", lang).lower() + ".",
        ),
    }
    return flows.get(generation_type, base)


def get_prompt_for_generation_type(generation_type: str, lang: str = "en") -> str:
    return get_flow_texts(generation_type, lang).initial_prompt


def get_second_prompt_for_generation_type(generation_type: str, lang: str = "en") -> str:
    return get_flow_texts(generation_type, lang).second_step_prompt or t("generation.second_step_text", lang)


def extract_document_media_type(document: Any) -> str:
    mime_type = (getattr(document, "mime_type", "") or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
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


def get_waiting_state_for_input_type(required_input_type: str):
    if required_input_type == "image":
        return GenerationStates.waiting_for_image
    if required_input_type == "video":
        return GenerationStates.waiting_for_video
    return GenerationStates.waiting_for_prompt


def build_invalid_input_message(required_input_type: str, generation_type: str, *, received_type: Optional[str] = None, lang: str = "en") -> str:
    flow_texts = get_flow_texts(generation_type, lang)
    if required_input_type == "text":
        return t("errors.formatted", lang, code=ErrorCode.E001_INVALID_INPUT_TYPE, message=t("errors.prompt_text_only", lang))
    if required_input_type == "image":
        if received_type == "video":
            return t("errors.formatted", lang, code=ErrorCode.E001_INVALID_INPUT_TYPE, message=flow_texts.invalid_specific_media)
        return t("errors.formatted", lang, code=ErrorCode.E001_INVALID_INPUT_TYPE, message=flow_texts.invalid_media)
    if required_input_type == "video":
        if received_type == "image":
            return t("errors.formatted", lang, code=ErrorCode.E001_INVALID_INPUT_TYPE, message=flow_texts.invalid_specific_media)
        return t("errors.formatted", lang, code=ErrorCode.E001_INVALID_INPUT_TYPE, message=flow_texts.invalid_media)
    return t("errors.formatted", lang, code=ErrorCode.E001_INVALID_INPUT_TYPE, message=t("errors.invalid_input_generic", lang))


def log_generation_error(
    error_code: str,
    *,
    generation_id: Any = None,
    user_id: Optional[int] = None,
    model_key: Optional[str] = None,
    status: str = "failed",
    details: Optional[str] = None,
) -> None:
    logger.error(
        {
            "action": "generation_error",
            "error_code": error_code,
            "generation_id": generation_id,
            "user_id": user_id,
            "model_key": model_key,
            "status": status,
            "details": details,
        }
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
    raw_items = state_data.get("input_media_items")
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


async def cleanup_media_items(media_items: list[dict[str, str]]) -> None:
    for item in media_items:
        local_path = item.get("local_path")
        if local_path:
            Path(local_path).unlink(missing_ok=True)


async def cleanup_state_media(state: FSMContext) -> None:
    state_data = await state.get_data()
    media_items = get_input_media_items(state_data)
    await cleanup_media_items(media_items)
    await state.update_data(input_media=None, input_media_items=[], input_image_file_id=None)


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


def build_input_audio_or_text_payload(message: Message) -> dict[str, str]:
    """Собрать текстовый или аудио-вход для озвучки."""
    text = (message.text or "").strip()
    if text:
        return {"type": "text", "text": text}
    if message.voice:
        return {"type": "voice", "file_id": message.voice.file_id}
    if message.audio:
        return {"type": "audio", "file_id": message.audio.file_id}
    if message.document and ((message.document.mime_type or "").lower().startswith("audio/")):
        return {"type": "audio", "file_id": message.document.file_id}
    return {}


def is_supported_media_document(document: Any, *, is_lipsync: bool) -> bool:
    """Проверить, что document подходит для текущего сценария генерации."""
    mime_type = (getattr(document, "mime_type", "") or "").lower()
    if is_lipsync:
        return mime_type.startswith("image/") or mime_type.startswith("video/")
    return mime_type.startswith("image/")


async def prompt_for_generation_input(message: Message, *, edit: bool, is_lipsync: bool) -> None:
    """Показать шаг загрузки media-входа с reply keyboard возврата к настройкам."""
    lang = get_actor_language(message.from_user)
    prompt_text = get_media_input_prompt_text(is_lipsync=is_lipsync, lang=lang)
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        t("generation.back_to_settings_hint", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


def build_generation_types_screen_text(lang: str = "en") -> str:
    """Собрать текст экрана выбора типа генерации."""
    details = "\n".join(
        f"• <b>{escape(get_generation_type_title(generation_type, lang))}</b> — {escape(get_generation_type_description(generation_type, lang))}"
        for generation_type in list_generation_types()
    )
    return f"{t('generation.choose_type', lang)}:\n\n{details}"


def build_generation_type_options() -> list[tuple[str, str]]:
    """Собрать опции клавиатуры выбора типа генерации."""
    ordered_generation_types = [
        generation_type
        for generation_type in ("text_to_image", "text_to_video", "image_edit", "image_to_video", "video_edit", "lipsync")
        if generation_type in set(list_generation_types())
    ]
    options = [
        (generation_type, GENERATION_TYPE_LABELS[generation_type])
        for generation_type in ordered_generation_types
    ]
    options.append(("all", GENERATION_TYPE_LABELS["all"]))
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
    if selected_generation_type and selected_generation_type != "all":
        return list_models_by_type(str(selected_generation_type))
    return []


async def render_models_screen(message: Message) -> None:
    """Показать список типов генерации."""
    lang = get_actor_language(message.from_user)
    await message.answer(
        build_generation_types_screen_text(lang),
        reply_markup=build_generation_sections_keyboard(lang),
        parse_mode="HTML",
    )


async def render_provider_screen(message: Message, *, edit: bool) -> None:
    """Показать список провайдеров для выбора моделей."""
    lang = get_actor_language(message.from_user)
    text = t("generation.choose_provider", lang)
    if edit:
        await message.edit_text(
            text,
            reply_markup=build_providers_keyboard(lang),
            parse_mode="HTML",
        )
        return
    await message.answer(
        text,
        reply_markup=build_providers_keyboard(lang),
        parse_mode="HTML",
    )


async def render_model_list_screen(
    message: Message,
    *,
    models: list[GenerationModel],
    edit: bool,
    heading: str,
    back_callback: str,
) -> None:
    """Показать список моделей для выбранного типа или провайдера."""
    lang = get_actor_language(message.from_user)
    if edit:
        await message.edit_text(
            heading,
            reply_markup=build_models_keyboard(models, back_callback, lang),
            parse_mode="HTML",
        )
        return
    await message.answer(
        heading,
        reply_markup=build_models_keyboard(models, back_callback, lang),
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


async def prompt_for_generation_image(message: Message, *, edit: bool, model: GenerationModel) -> None:
    """Показать шаг загрузки изображения с reply keyboard возврата к настройкам."""
    lang = get_actor_language(message.from_user)
    prompt_text = t("generation.image_for_model", lang, model=model.title)
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        t("generation.back_to_settings_hint", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


async def prompt_for_generation_images(message: Message, *, edit: bool, model: GenerationModel) -> None:
    lang = get_actor_language(message.from_user)
    prompt_text = t(
        "generation.images_for_model",
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
        t("generation.back_to_settings_hint", lang),
        reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
    )


async def prompt_for_generation_video(message: Message, *, edit: bool, model: GenerationModel) -> None:
    """Показать шаг загрузки видео с reply keyboard возврата к настройкам."""
    lang = get_actor_language(message.from_user)
    prompt_text = t("generation.video_for_model", lang, model=model.title)
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        t("generation.back_to_settings_hint", lang),
        reply_markup=build_back_to_settings_keyboard(lang),
    )


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
    required_input_type = get_required_input_type(model.generation_type) if model else "text"
    prompt = (state_data.get("prompt") or "").strip()
    input_media = state_data.get("input_media")
    input_media_items = get_input_media_items(state_data)
    input_audio_or_text = state_data.get("input_audio_or_text")

    user_repo = UserRepository(session)
    user = await user_repo.get_or_create_user_from_telegram(telegram_user)
    lang = get_user_preferred_language(user, telegram_user)

    if is_lipsync:
        prompt = get_input_audio_or_text_display(input_audio_or_text, lang)
        is_complete = bool(model_key and input_media and input_audio_or_text)
    else:
        is_complete = bool(model_key and prompt)
        if model and model.input_media_field == "images":
            has_legacy_single_image = bool(input_media and input_media.get("type") in {"photo", "image", "images"})
            is_complete = is_complete and (len(input_media_items) >= model.min_images or has_legacy_single_image)
        elif required_input_type == "image":
            is_complete = is_complete and bool(input_media and input_media.get("type") in {"photo", "image"})
        elif required_input_type == "video":
            is_complete = is_complete and bool(input_media and input_media.get("type") == "video")

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
    media_items = get_input_media_items(state_data)
    lang = get_state_language(state_data, message.from_user)
    if not model:
        await message.answer(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang))
        return
    if len(media_items) < model.min_images:
        await message.answer(
            format_user_error(ErrorCode.E003_MISSING_IMAGE, t("generation.need_min_images", lang, count=model.min_images), lang),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
        )
        return
    await state.update_data(input_media={"type": "images", "count": len(media_items)})
    await set_waiting_for_prompt_with_diagnostic(
        state,
        user_id=message.from_user.id,
        incoming_text_type=get_incoming_text_type(message),
    )
    await message.answer(
        get_second_prompt_for_generation_type(model.generation_type, get_actor_language(message.from_user)),
        reply_markup=build_back_to_settings_keyboard(get_actor_language(message.from_user)),
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


async def safe_send_bot_message(bot, chat_id: int, text: str, reply_markup=None) -> None:
    """Безопасно отправить сообщение пользователю, не роняя background task."""
    if not text or not text.strip():
        logger.warning("Skipped sending empty Telegram message to user")
        return
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as exc:
        logger.exception("Failed to send Telegram message to user: %s", type(exc).__name__)


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
        return "en"
    try:
        async with db_manager.session_factory() as session:
            user = await UserRepository(session).get_user_profile(user_id)
            if user is None:
                return "en"
            return get_user_language(user.language_code)
    except Exception:
        return "en"


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
    send_results_as_files = delivery_preference if delivery_preference is not None else False
    if delivery_preference is None and user_id is not None:
        send_results_as_files = await get_user_send_results_as_files(user_id)
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
    logger.info(
        {
            "action": "generation_output_delivery",
            "user_id": user_id,
            "send_results_as_files": send_results_as_files,
            "content_type": normalize_content_type(content_type),
            "delivery_method": delivery_method,
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
    """Нормализовать имя output-файла Wavespeed к формату imai-*.ext."""
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
    temp_input_path: Optional[str] | list[str],
) -> None:
    semaphore = asyncio.Semaphore(4)

    async def _run_child(generation_request_id, prediction_id: str) -> None:
        async with semaphore:
            await _run_single_generation_request(
                bot=bot,
                user_id=user_id,
                chat_id=chat_id,
                generation_request_id=generation_request_id,
                prediction_id=prediction_id,
                model_key=model_key,
                cost=GENERATION_COST,
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
        lang = get_actor_language(message.from_user)
        current_state = await state.get_state()
        active_count = await count_active_generations_for_user(message.from_user.id, session)
        limit = settings.max_parallel_generations_per_user
        if active_count >= limit:
            log_parallel_generation_event("parallel_generation_limit_reached", message.from_user.id, active_count, limit)
            await message.answer(
                t("generation.parallel_limit_reached", lang, count=active_count),
                reply_markup=get_main_menu_keyboard(lang),
            )
            return
        log_parallel_generation_event("parallel_generation_allowed", message.from_user.id, active_count, limit)

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
        await state.update_data(selected_generation_type=None, selected_provider=None)
        await render_models_screen(message)
        
        logger.debug(f"Generation menu shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_generation_menu: %s", e)
        await message.answer(t("generation.menu_open_error", get_actor_language(message.from_user)))


@router.callback_query(lambda cb: cb.data.startswith(MODEL_PREFIX))
async def choose_generation_model(callback: CallbackQuery, state: FSMContext):
    """Выбрать модель для генерации."""
    log_generation_callback(callback)
    model_token = callback.data.removeprefix(MODEL_PREFIX)
    state_data = await state.get_data()
    model_key = resolve_model_key_from_token(get_selected_models_for_state(state_data), model_token) or model_token
    model = get_generation_model(model_key)
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
        input_audio_or_text=None,
        prompt=None,
    )
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith(GENERATION_SECTION_PREFIX))
async def choose_generation_section(callback: CallbackQuery, state: FSMContext):
    """Выбрать раздел генерации и показать список моделей."""
    log_generation_callback(callback)
    generation_type = callback.data.removeprefix(GENERATION_SECTION_PREFIX)
    models = list_models_by_type(generation_type)
    await state.set_state(GenerationStates.choosing_generation_type)
    await state.update_data(selected_generation_type=generation_type, selected_provider=None)
    if not models:
        lang = get_actor_language(callback.from_user)
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
        heading=f"{t('generation.choose_model', get_actor_language(callback.from_user))}:",
        back_callback=BACK_TO_SECTIONS,
    )
    await callback.answer()


@router.callback_query(F.data == GENERATION_ALL)
async def show_all_generation_providers(callback: CallbackQuery, state: FSMContext):
    """Показать список провайдеров для All List."""
    log_generation_callback(callback)
    await state.set_state(GenerationStates.choosing_provider)
    await state.update_data(selected_generation_type="all", selected_provider=None, current_screen="providers")
    await render_provider_screen(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data == BACK_TO_SECTIONS)
async def back_to_generation_sections(callback: CallbackQuery, state: FSMContext):
    """Вернуться к выбору раздела генерации."""
    log_generation_callback(callback)
    await state.set_state(GenerationStates.choosing_generation_type)
    await state.update_data(selected_generation_type=None, selected_provider=None)
    lang = get_actor_language(callback.from_user)
    await callback.message.edit_text(
        build_generation_types_screen_text(lang),
        reply_markup=build_generation_sections_keyboard(lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(PROVIDER_PREFIX))
async def choose_provider(callback: CallbackQuery, state: FSMContext):
    """Выбрать провайдера и показать его модели."""
    log_generation_callback(callback)
    provider = callback.data.removeprefix(PROVIDER_PREFIX)
    if provider not in list_providers():
        await callback.answer(t("generation.provider_unavailable", get_actor_language(callback.from_user)), show_alert=True)
        return
    models = list_models_by_provider(provider)
    if not models:
        await state.set_state(GenerationStates.choosing_provider)
        await state.update_data(selected_generation_type="all", selected_provider=None)
        lang = get_actor_language(callback.from_user)
        await callback.message.edit_text(
            t("generation.no_models_in_provider", lang),
            reply_markup=build_providers_keyboard(lang),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.set_state(GenerationStates.choosing_provider)
    await state.update_data(selected_generation_type="all", selected_provider=provider)
    await render_model_list_screen(
        callback.message,
        models=models,
        edit=True,
        heading=f"{t('generation.choose_model', get_actor_language(callback.from_user))}:",
        back_callback=BACK_TO_PROVIDERS,
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == SETTINGS_BACK_MODELS)
async def back_to_generation_models(callback: CallbackQuery, state: FSMContext):
    """Вернуться к предыдущему экрану выбора модели."""
    log_generation_callback(callback)
    state_data = await state.get_data()
    selected_provider = state_data.get("selected_provider")
    selected_generation_type = state_data.get("selected_generation_type")

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
            heading=f"{t('generation.choose_model', get_actor_language(callback.from_user))}:",
            back_callback=BACK_TO_PROVIDERS,
        )
        await callback.answer()
        return

    if selected_generation_type and selected_generation_type != "all":
        await state.set_state(GenerationStates.choosing_generation_type)
        await render_model_list_screen(
            callback.message,
            models=list_models_by_type(str(selected_generation_type)),
            edit=True,
            heading=f"{t('generation.choose_model', get_actor_language(callback.from_user))}:",
            back_callback=BACK_TO_SECTIONS,
        )
        await callback.answer()
        return

    await back_to_generation_sections(callback, state)


@router.callback_query(F.data == BACK_TO_PROVIDERS)
async def back_to_generation_providers(callback: CallbackQuery, state: FSMContext):
    """Вернуться к списку провайдеров."""
    log_generation_callback(callback)
    await state.set_state(GenerationStates.choosing_provider)
    await state.update_data(selected_generation_type="all", selected_provider=None)
    await render_provider_screen(callback.message, edit=True)
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(SETTINGS_OPEN_PREFIX))
async def open_setting_selector(callback: CallbackQuery, state: FSMContext):
    """Открыть выбор значения настройки модели."""
    log_generation_callback(callback)
    lang = get_actor_language(callback.from_user)
    setting_key = callback.data.removeprefix(SETTINGS_OPEN_PREFIX)
    if not setting_key:
        await callback.answer(t("generation.setting_not_found", lang), show_alert=True)
        return
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    if not model_key:
        await callback.message.edit_text(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("generation.model_not_selected", lang), lang), reply_markup=None)
        await callback.answer()
        return
    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await callback.answer(t("generation.setting_not_found", lang), show_alert=True)
        return
    setting = model.user_settings[setting_key]
    await state.update_data(current_setting_key=setting_key)
    if setting.type == "text":
        user_settings = get_model_state_settings(state_data, model_key)
        current_value = str(user_settings.get(setting_key, setting.default))
        await state.set_state(GenerationStates.waiting_for_setting_text)
        await callback.message.edit_text(
            build_setting_value_text(model, setting_key, current_value, lang),
            reply_markup=None,
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
    lang = get_actor_language(message.from_user)
    await message.answer(t("generation.back_to_model_settings", lang), reply_markup=get_main_menu_keyboard(lang))
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
    user_settings[str(setting_key)] = value
    await state.update_data(user_settings=user_settings, current_setting_key=None)
    await state.set_state(GenerationStates.choosing_settings)
    await message.answer(t("generation.value_saved", lang), reply_markup=get_main_menu_keyboard(lang))
    await render_settings_screen_message(message, state, edit=False)


@router.callback_query(lambda cb: cb.data.startswith(SETTINGS_VALUE_PREFIX))
async def choose_setting_value(callback: CallbackQuery, state: FSMContext):
    """Сохранить выбранное значение настройки и вернуться к экрану настроек."""
    log_generation_callback(callback)
    lang = get_actor_language(callback.from_user)
    setting_payload = callback.data.removeprefix(SETTINGS_VALUE_PREFIX)
    if ":" not in setting_payload:
        await callback.answer(t("generation.invalid_value", lang), show_alert=True)
        return
    setting_key, option_index_raw = setting_payload.rsplit(":", 1)
    if not option_index_raw.isdigit():
        await callback.answer(t("generation.invalid_value", lang), show_alert=True)
        return
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    if not model_key:
        await callback.message.edit_text(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("generation.model_not_selected", lang), lang), reply_markup=None)
        await callback.answer()
        return

    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await callback.answer(t("generation.setting_not_found", lang), show_alert=True)
        return

    user_settings = get_model_state_settings(state_data, model_key)
    option_index = int(option_index_raw)
    options = model.user_settings[setting_key].options
    if option_index < 0 or option_index >= len(options):
        await callback.answer(t("generation.invalid_value", lang), show_alert=True)
        return
    selected_value = str(options[option_index].value)
    user_settings[setting_key] = selected_value
    await state.update_data(user_settings=user_settings, current_setting_key=None)
    await state.set_state(GenerationStates.choosing_settings)
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.callback_query(lambda cb: cb.data == SETTINGS_CONTINUE)
async def continue_after_settings(callback: CallbackQuery, state: FSMContext):
    """Перейти от настроек к следующему шагу ввода по типу модели."""
    log_generation_callback(callback)
    state_data = await state.get_data()
    lang = get_state_language(state_data, callback.from_user)
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    if not model:
        if state_data.get("model_generation_type") == "lipsync":
            await state.set_state(GenerationStates.waiting_for_image)
            await prompt_for_generation_input(callback.message, edit=True, is_lipsync=True)
            await callback.answer()
            return
        await callback.message.edit_text(
            format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, t("errors.model_unavailable", lang), lang),
            reply_markup=None,
        )
        await callback.answer()
        return

    required_input_type = get_required_input_type(model.generation_type)
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
            await state.set_state(GenerationStates.waiting_for_images)
            await prompt_for_generation_images(callback.message, edit=True, model=model)
        else:
            await state.set_state(GenerationStates.waiting_for_image)
            await prompt_for_generation_image(callback.message, edit=True, model=model)
    elif required_input_type == "video":
        await state.set_state(GenerationStates.waiting_for_video)
        await prompt_for_generation_video(callback.message, edit=True, model=model)
    else:
        await state.set_state(GenerationStates.waiting_for_image)
        await prompt_for_generation_input(callback.message, edit=True, is_lipsync=True)
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

    await cleanup_state_media(state)
    await state.update_data(
        prompt=None,
        input_audio_or_text=None,
        input_media_urls=[],
        input_media_paths=[],
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
        await message.answer(t("generation.back_to_sections", get_actor_language(message.from_user)), reply_markup=get_main_menu_keyboard(get_actor_language(message.from_user)))
        await render_models_screen(message)
        return

    await state.update_data(
        model_key=model.key,
        model_title=model.title,
        model_endpoint=model.endpoint,
        model_generation_type=model.generation_type,
        user_settings=selected_settings,
    )
    await state.set_state(GenerationStates.choosing_settings)
    await message.answer(t("generation.back_to_model_settings", get_actor_language(message.from_user)), reply_markup=get_main_menu_keyboard(get_actor_language(message.from_user)))
    await message.answer(
        build_settings_text(model, selected_settings, get_actor_language(message.from_user)),
        reply_markup=build_model_settings_keyboard(model, selected_settings, get_actor_language(message.from_user)),
        parse_mode="HTML",
    )


@router.message(
    StateFilter(
        GenerationStates.waiting_for_image,
        GenerationStates.waiting_for_images,
        GenerationStates.waiting_for_video,
        GenerationStates.waiting_for_prompt,
    ),
    lambda message: is_localized_button_text(message.text, "common.back_to_settings", getattr(message.from_user, "language_code", None)),
)
async def back_to_settings_from_input_step(message: Message, state: FSMContext):
    """Вернуться с этапа ввода к настройкам модели без запуска генерации."""
    await navigate_back_to_settings(message, state)


@router.message(GenerationStates.waiting_for_image, lambda message: bool(message.photo) or bool(message.document) or bool(message.video))
async def process_generation_image(message: Message, state: FSMContext):
    """Принять изображение для генерации."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    is_lipsync = is_lipsync_generation_state(state_data)
    model_generation_type = str(state_data.get("model_generation_type") or "image_edit")
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
            build_invalid_input_message("image", model_generation_type, received_type=received_type, lang=get_actor_language(message.from_user)),
            reply_markup=build_back_to_settings_keyboard(get_actor_language(message.from_user)),
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
        input_image_file_id=media_item.get("file_id"),
    )
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
    if not is_supported_image_input(message):
        await message.answer(
            build_invalid_input_message("image", model_generation_type, received_type=received_type, lang=get_actor_language(message.from_user)),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=get_actor_language(message.from_user)),
        )
        return

    media_items = get_input_media_items(state_data)
    if len(media_items) >= model.max_images:
        await message.answer(
            t("generation.image_limit_reached", lang, count=model.max_images),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
        )
        return

    try:
        media_item = await upload_message_media_item(message)
    except ImageUploadError:
        log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=message.from_user.id, status="failed")
        await message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, t("errors.prepare_image_failed", lang), lang),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
        )
        return

    media_items.append(media_item)
    await state.update_data(
        input_media_items=media_items,
        input_media={"type": "images", "count": len(media_items)},
        input_image_file_id=media_items[0].get("file_id"),
    )
    if len(media_items) >= model.max_images:
        await set_waiting_for_prompt_with_diagnostic(
            state,
            user_id=message.from_user.id,
            incoming_text_type=get_incoming_text_type(message),
        )
        await message.answer(
            get_second_prompt_for_generation_type(model_generation_type, get_actor_language(message.from_user)),
            reply_markup=build_back_to_settings_keyboard(get_actor_language(message.from_user)),
        )
        return

    await message.answer(
        t("generation.images_uploaded_progress", lang, count=len(media_items), max_count=model.max_images),
        reply_markup=build_media_upload_reply_keyboard(show_continue=True, lang=lang),
    )


@router.message(GenerationStates.waiting_for_image)
async def invalid_generation_image(message: Message, state: FSMContext):
    """Сообщить, что ожидается изображение."""
    state_data = await state.get_data()
    lang = get_state_language(state_data, message.from_user)
    is_lipsync = is_lipsync_generation_state(state_data)
    await message.answer(
        t("generation.invalid_wait_lipsync", lang) if is_lipsync else t("generation.invalid_wait_image", lang),
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
            build_invalid_input_message("video", model_generation_type, received_type=received_type, lang=get_actor_language(message.from_user)),
            reply_markup=build_back_to_settings_keyboard(get_actor_language(message.from_user)),
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
        input_image_file_id=media_item.get("file_id"),
    )
    await set_waiting_for_prompt_with_diagnostic(
        state,
        user_id=message.from_user.id,
        incoming_text_type=get_incoming_text_type(message),
    )
    await message.answer(
        get_second_prompt_for_generation_type(model_generation_type, get_actor_language(message.from_user)),
        reply_markup=build_back_to_settings_keyboard(get_actor_language(message.from_user)),
    )


@router.message(GenerationStates.waiting_for_video)
async def invalid_generation_video(message: Message, state: FSMContext):
    """Сообщить, что ожидается видео."""
    state_data = await state.get_data()
    model_generation_type = str(state_data.get("model_generation_type") or "video_edit")
    await message.answer(
        build_invalid_input_message("video", model_generation_type, lang=get_actor_language(message.from_user)),
        reply_markup=build_back_to_settings_keyboard(get_actor_language(message.from_user)),
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
        required_input_type = get_required_input_type(model.generation_type)
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
                await message.answer(format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, t("generation.lipsync_need_text", lang), lang))
                return
            if not input_media:
                await message.answer(
                    format_user_error(ErrorCode.E003_MISSING_IMAGE, t("generation.lipsync_need_media", lang), lang),
                    reply_markup=build_back_to_settings_keyboard(lang),
                )
                await state.set_state(GenerationStates.waiting_for_image)
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
            log_generation_error(ErrorCode.E006_INSUFFICIENT_BALANCE, user_id=user.id, model_key=model_key, status="rejected")
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
            await message.answer(
                format_user_error(
                    ErrorCode.E006_INSUFFICIENT_BALANCE,
                    t("errors.insufficient_balance_details", lang, cost=total_cost, balance=user.balance),
                    lang,
                ),
            )
            await message.answer(
                t("generation.adjust_or_top_up", lang),
                reply_markup=get_main_menu_keyboard(lang),
            )
            await render_settings_screen_message(message, state, edit=False)
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
    prompt = state_data.get("prompt")
    input_media = state_data.get("input_media")
    input_media_items = get_input_media_items(state_data)
    input_image_file_id = state_data.get("input_image_file_id")
    is_lipsync = is_lipsync_generation_state(state_data)
    model = get_generation_model(model_key) if model_key else None
    required_input_type = get_required_input_type(model.generation_type) if model else "text"
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

    is_complete = bool(model_key and model_title and model_endpoint and prompt)
    if model and model.input_media_field == "images":
        has_legacy_single_image = bool(input_media and input_media.get("type") in {"photo", "image", "images"})
        is_complete = is_complete and (len(input_media_items) >= model.min_images or has_legacy_single_image)
    elif required_input_type == "image":
        is_complete = is_complete and bool(input_media and input_media.get("type") in {"photo", "image"})
    elif required_input_type == "video":
        is_complete = is_complete and bool(input_media and input_media.get("type") == "video")

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
    total_cost = GENERATION_COST

    try:
        user_repo = UserRepository(session)
        generation_repo = GenerationRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
        lang = get_user_preferred_language(user, callback.from_user)
        await state.update_data(user_language=lang)
        debited_user_id = user.id
        num_generations = get_model_num_generations(model, user_settings)
        total_cost = get_total_generation_cost(model, user_settings)
        active_count = await generation_repo.count_active_generations(user.id)
        limit = settings.max_parallel_generations_per_user
        if active_count >= limit:
            log_parallel_generation_event("parallel_generation_limit_reached", user.id, active_count, limit)
            await callback.message.answer(
                t("generation.parallel_limit_reached", lang, count=active_count),
                reply_markup=get_main_menu_keyboard(lang),
            )
            await callback.answer()
            return
        if active_count + num_generations > limit:
            log_parallel_generation_event("parallel_generation_limit_reached", user.id, active_count, limit)
            await callback.message.answer(
                t("generation.parallel_limit_would_exceed", lang, limit=limit),
                reply_markup=get_main_menu_keyboard(lang),
            )
            await callback.answer()
            return
        log_parallel_generation_event("parallel_generation_allowed", user.id, active_count, limit)

        if not await user_repo.decrease_balance(user.id, total_cost):
            log_balance_event("insufficient_balance", user.id, total_cost)
            log_generation_error(ErrorCode.E006_INSUFFICIENT_BALANCE, user_id=user.id, model_key=model_key, status="rejected")
            log_generation_diagnostic(
                action="insufficient_balance",
                user_id=user.id,
                state_value=await state.get_state(),
                state_data=state_data,
                incoming_text_type=get_incoming_text_type(is_callback=True),
                prompt=str(prompt or ""),
                total_cost=total_cost,
            )
            await callback.message.answer(
                format_user_error(
                    ErrorCode.E006_INSUFFICIENT_BALANCE,
                    t("errors.insufficient_balance_details", lang, cost=total_cost, balance=user.balance),
                    lang,
                ),
                reply_markup=get_main_menu_keyboard(lang),
            )
            await reset_generation_state(state)
            await callback.answer()
            return
        debited_balance = True
        log_balance_event("balance_debited", user.id, total_cost)

        media_urls: list[str] = []
        if input_media_items:
            media_urls = [item["public_url"] for item in input_media_items if item.get("public_url")]
            temp_input_path = [item["local_path"] for item in input_media_items if item.get("local_path")]
        elif model and model_requires_media(model) and input_media:
            telegram_files = TelegramFilesService(callback.bot)
            temp_media = await telegram_files.download_temp_file_and_get_public_url(str(input_media.get("file_id")))
            media_urls = [temp_media.public_url]
            temp_input_path = str(temp_media.local_path)
        payload = build_payload(model_key, media_urls, prompt, user_settings)

        for _ in range(num_generations):
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
                cost=GENERATION_COST,
            )
            generation_request_ids.append(generation_request.id)

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
                    cost=GENERATION_COST,
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
            (
                f"{t('generation.started_count', lang, count=num_generations)}\n\n"
                f"{t('generation.model_label', lang, model=escape(model_title))}"
            ),
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
        await state.update_data(input_image_file_id=None, input_media=None, input_media_items=[])
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
        await callback.message.answer(
            format_user_error(ErrorCode.E011_INVALID_MODEL_SETTINGS, t("errors.invalid_model_settings", lang), lang),
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
async def handle_unknown_generation_callback(callback: CallbackQuery, state: FSMContext):
    """Fallback для устаревших или неподдерживаемых inline-кнопок генераций."""
    log_generation_callback(callback)
    lang = get_actor_language(callback.from_user)
    await state.set_state(GenerationStates.choosing_generation_type)
    await state.update_data(selected_generation_type=None, selected_provider=None)
    await callback.answer(t("generation.legacy_button", lang), show_alert=True)
    await callback.message.edit_text(
        build_generation_types_screen_text(lang),
        reply_markup=build_generation_sections_keyboard(lang),
        parse_mode="HTML",
    )
