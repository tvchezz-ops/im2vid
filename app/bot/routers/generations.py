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
    resolve_model_key_from_token,
)
from app.bot.states import GenerationStates
from app.config import settings
from app.db import GenerationRepository, UserRepository
from app.db.session import db_manager
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

ACTIVE_GENERATIONS: Dict[int, Dict[str, Any]] = {}
POLL_TIMEOUT_SECONDS = 600
GENERATION_COST = 1
DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS = 3600
DOCUMENT_SEND_RETRY_COUNT = 3
OUTPUT_DOWNLOAD_TIMEOUT_SECONDS = 300

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


def format_user_error(code: str, message: str) -> str:
    return f"❌ Ошибка {code}: {message}"


@dataclass(frozen=True)
class FlowTexts:
    initial_prompt: str
    second_step_prompt: str = ""
    missing_prompt: str = "требуется текстовый prompt."
    missing_media: str = ""
    invalid_media: str = ""
    invalid_specific_media: str = ""


@dataclass(frozen=True)
class OutputDeliveryResult:
    delivered_successfully: bool
    use_r2: bool = False


FLOW_TEXTS = {
    "text_to_image": FlowTexts(
        initial_prompt="Опишите изображение, которое хотите создать.",
    ),
    "text_to_video": FlowTexts(
        initial_prompt="Опишите видео, которое хотите создать.",
    ),
    "image_edit": FlowTexts(
        initial_prompt="Отправьте изображение, которое хотите изменить. После этого бот попросит текстовое описание.",
        second_step_prompt="Теперь отправьте текстовое описание.",
        missing_media="требуется изображение.",
        invalid_media="нужно отправить изображение.",
        invalid_specific_media="Нужно отправить изображение, не видео.",
    ),
    "image_to_video": FlowTexts(
        initial_prompt="Отправьте изображение, которое хотите анимировать. После этого бот попросит текстовое описание.",
        second_step_prompt="Теперь отправьте текстовое описание.",
        missing_media="требуется изображение.",
        invalid_media="нужно отправить изображение.",
        invalid_specific_media="Нужно отправить изображение, не видео.",
    ),
    "video_edit": FlowTexts(
        initial_prompt="Отправьте видео, которое хотите изменить. После этого бот попросит текстовое описание.",
        second_step_prompt="Теперь отправьте текстовое описание.",
        missing_media="требуется видео.",
        invalid_media="нужно отправить видео.",
        invalid_specific_media="Нужно отправить видео, не изображение.",
    ),
    "lipsync": FlowTexts(
        initial_prompt="Вы выбрали Lipsync.\nОтправьте фото или видео, затем текст или голос для озвучки.",
        second_step_prompt="Теперь отправьте текст или голосовое сообщение для озвучки.",
    ),
}

GENERATION_TYPE_LABELS = {
    "text_to_image": "🖼 Text → Image",
    "text_to_video": "🎬 Text → Video",
    "image_edit": "🧩 Image Edit",
    "image_to_video": "🎥 Image → Video",
    "video_edit": "🎞 Video Edit",
    "lipsync": "🗣 Lipsync",
    "all": "📚 All models",
}

GENERATION_TYPE_TITLES = {
    "text_to_image": "Text → Image",
    "text_to_video": "Text → Video",
    "image_edit": "Image Edit",
    "image_to_video": "Image → Video",
    "video_edit": "Video Edit",
    "lipsync": "Lipsync (озвучка лица)",
}

GENERATION_TYPE_DESCRIPTIONS = {
    "text_to_image": "Создание изображения по тексту",
    "text_to_video": "Создание видео по тексту",
    "image_edit": "Редактирование или преобразование изображения",
    "image_to_video": "Анимация изображения в видео",
    "video_edit": "Преобразование или стилизация видео",
    "lipsync": "Анимация лица под аудио или текст",
}

PROVIDER_LABELS = {
    "alibaba": "Alibaba",
    "openai": "OpenAI",
    "bytedance": "ByteDance",
    "google": "Google",
}


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
        return "—"
    return "\n".join(
        f"- <b>{escape(setting.title)}</b>: <code>{escape(str(user_settings.get(setting.key, setting.default)))}</code>"
        for setting in model.user_settings.values()
    )


def get_total_generation_cost(model: GenerationModel, user_settings: dict[str, Any]) -> int:
    return GENERATION_COST * get_model_num_generations(model, user_settings)


def build_settings_text(model: GenerationModel, user_settings: dict[str, Any]) -> str:
    """Собрать экран настроек модели."""
    if not model.user_settings:
        return (
            f"Настройки модели: <b>{escape(model.title)}</b>\n\n"
            "У этой модели нет дополнительных настроек. Можно продолжить."
        )
    return (
        f"Настройки модели: <b>{escape(model.title)}</b>\n\n"
        f"Выберите параметры или нажмите Продолжить.\n\n"
        f"Текущие значения:\n{format_generation_settings(model, user_settings)}"
    )


def build_setting_value_text(model: GenerationModel, setting_key: str, current_value: str) -> str:
    """Собрать экран выбора конкретной настройки."""
    setting = model.user_settings[setting_key]
    if setting.type == "text":
        description_block = f"\n\n{escape(setting.description)}" if setting.description else ""
        return (
            f"Настройки модели: <b>{escape(model.title)}</b>\n\n"
            f"Параметр: <b>{escape(setting.title)}</b>\n"
            f"Текущее значение: <code>{escape(current_value)}</code>\n\n"
            f"Отправьте новое текстовое значение сообщением. Отправьте <code>-</code>, чтобы очистить поле.{description_block}"
        )
    options = "\n".join(f"• <code>{escape(option.value)}</code>" for option in setting.options)
    return (
        f"Настройки модели: <b>{escape(model.title)}</b>\n\n"
        f"Выберите значение параметра.\n\n"
        f"Модель: <b>{escape(model.title)}</b>\n"
        f"Параметр: <b>{escape(setting.title)}</b>\n"
        f"Текущее значение: <code>{escape(current_value)}</code>\n\n"
        f"Доступные варианты:\n{options}"
    )


def build_confirmation_text(
    model: GenerationModel,
    user_settings: dict[str, Any],
    prompt: str,
    balance: int,
) -> str:
    """Собрать экран подтверждения генерации."""
    num_generations = get_model_num_generations(model, user_settings)
    total_cost = get_total_generation_cost(model, user_settings)
    balance_after_launch = max(balance - total_cost, 0)
    prompt_label = "Prompt"
    if model.generation_type == "lipsync":
        prompt_label = "Озвучка"
    return (
        f"Проверьте генерацию:\n\n"
        f"Модель: <b>{escape(model.title)}</b>\n"
        f"Настройки:\n{format_generation_settings(model, user_settings)}\n\n"
        f"{prompt_label}: <i>{escape(prompt)}</i>\n\n"
        f"Количество генераций: <code>{num_generations}</code>\n"
        f"Стоимость: {total_cost} кредитов\n"
        f"Баланс после запуска: <code>{balance_after_launch}</code>"
    )


def build_partial_generation_failed_message() -> str:
    return "❌ Ошибка E007: одна из генераций не удалась. 1 кредит возвращён."


def get_user_friendly_error_message(error: Exception, result: Optional[WavespeedResult] = None) -> str:
    """Вернуть безопасное и понятное сообщение об ошибке для пользователя."""
    if isinstance(error, WavespeedTimeoutError):
        return "⏱ Ошибка E008: генерация заняла слишком много времени. Кредит возвращён."

    if isinstance(error, WavespeedFailedError) or (result is not None and result.status == "failed"):
        return "❌ Ошибка E007: генерация не удалась. Кредит возвращён.\n\nПричина: запрос отклонён провайдером."

    if isinstance(error, TelegramBadRequest):
        return format_user_error(ErrorCode.E009_TELEGRAM_DELIVERY_FAILED, "Telegram не смог доставить результат.")

    if isinstance(error, (WavespeedNetworkError, aiohttp.ClientError, TimeoutError)):
        return format_user_error(ErrorCode.E010_INTERNAL_ERROR, "сбой сети при получении результата.")

    return format_user_error(ErrorCode.E010_INTERNAL_ERROR, "внутренняя ошибка. Попробуйте ещё раз.")


class OutputDeliveryTooLargeError(Exception):
    """Файл результата слишком большой для отправки в Telegram."""


def get_model_state_settings(state_data: dict[str, Any], model_key: str) -> dict[str, Any]:
    """Получить провалидированные настройки модели из FSM."""
    return validate_model_settings(model_key, state_data.get("user_settings"))


def is_lipsync_generation_state(state_data: dict[str, Any]) -> bool:
    """Определить, что текущий сценарий относится к lipsync."""
    return state_data.get("model_generation_type") == "lipsync"


def get_input_audio_or_text_display(value: Any) -> str:
    """Вернуть пользовательское описание текстового или аудио-входа."""
    if not isinstance(value, dict):
        return ""
    source_type = value.get("type")
    if source_type == "text":
        return str(value.get("text") or "")
    if source_type == "voice":
        return "Голосовое сообщение"
    if source_type == "audio":
        return "Аудиофайл"
    return ""


def get_media_input_prompt_text(*, is_lipsync: bool) -> str:
    """Вернуть текст шага загрузки media-входа."""
    if is_lipsync:
        return FLOW_TEXTS["lipsync"].initial_prompt
    return FLOW_TEXTS["image_edit"].initial_prompt


def get_lipsync_incomplete_error_text() -> str:
    """Вернуть единое сообщение о неполных входных данных lipsync."""
    return "❌ Для lipsync нужно изображение/видео и текст или аудио."


def get_second_step_prompt_text(*, is_lipsync: bool) -> str:
    """Вернуть текст второго шага после загрузки media."""
    if is_lipsync:
        return FLOW_TEXTS["lipsync"].second_step_prompt
    return "Теперь отправьте текстовое описание."


def get_flow_texts(generation_type: str) -> FlowTexts:
    return FLOW_TEXTS.get(generation_type, FLOW_TEXTS["text_to_image"])


def get_prompt_for_generation_type(generation_type: str) -> str:
    return get_flow_texts(generation_type).initial_prompt


def get_second_prompt_for_generation_type(generation_type: str) -> str:
    return get_flow_texts(generation_type).second_step_prompt or "Теперь отправьте текстовое описание."


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


def build_invalid_input_message(required_input_type: str, generation_type: str, *, received_type: Optional[str] = None) -> str:
    flow_texts = get_flow_texts(generation_type)
    if required_input_type == "text":
        return format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, "на этом этапе нужен только текстовый prompt.")
    if required_input_type == "image":
        if received_type == "video":
            return format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, flow_texts.invalid_specific_media)
        return format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, flow_texts.invalid_media)
    if required_input_type == "video":
        if received_type == "image":
            return format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, flow_texts.invalid_specific_media)
        return format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, flow_texts.invalid_media)
    return format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, "некорректный тип входных данных.")


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
    prompt_text = get_media_input_prompt_text(is_lipsync=is_lipsync)
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        "Если передумали, можно вернуться к настройкам.",
        reply_markup=build_back_to_settings_keyboard(),
    )


def build_generation_types_screen_text() -> str:
    """Собрать текст экрана выбора типа генерации."""
    details = "\n".join(
        f"• <b>{escape(GENERATION_TYPE_TITLES[generation_type])}</b> — {escape(GENERATION_TYPE_DESCRIPTIONS[generation_type])}"
        for generation_type in list_generation_types()
    )
    return f"Выберите тип генерации:\n\n{details}"


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
    await message.answer(
        build_generation_types_screen_text(),
        reply_markup=build_generation_sections_keyboard(),
        parse_mode="HTML",
    )


async def render_provider_screen(message: Message, *, edit: bool) -> None:
    """Показать список провайдеров для выбора моделей."""
    text = "Выберите провайдера:"
    if edit:
        await message.edit_text(
            text,
            reply_markup=build_providers_keyboard(),
            parse_mode="HTML",
        )
        return
    await message.answer(
        text,
        reply_markup=build_providers_keyboard(),
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
    if edit:
        await message.edit_text(
            heading,
            reply_markup=build_models_keyboard(models, back_callback),
            parse_mode="HTML",
        )
        return
    await message.answer(
        heading,
        reply_markup=build_models_keyboard(models, back_callback),
        parse_mode="HTML",
    )


async def render_settings_screen(message: Message, state: FSMContext) -> None:
    """Показать экран настроек выбранной модели."""
    await render_settings_screen_message(message, state, edit=True)


async def render_settings_screen_message(message: Message, state: FSMContext, *, edit: bool) -> None:
    """Показать экран настроек выбранной модели через edit или обычное сообщение."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    if not model_key:
        if edit:
            await message.edit_text("❌ Модель не выбрана. Начни заново.", reply_markup=None)
        else:
            await message.answer("❌ Модель не выбрана. Начни заново.", reply_markup=get_main_menu_keyboard())
        await message.answer("🏠 Главное меню", reply_markup=get_main_menu_keyboard())
        return

    model = get_generation_model(model_key)
    user_settings = get_model_state_settings(state_data, model_key)
    if edit:
        await message.edit_text(
            build_settings_text(model, user_settings),
            reply_markup=build_model_settings_keyboard(model, user_settings),
            parse_mode="HTML",
        )
        return

    await message.answer(
        build_settings_text(model, user_settings),
        reply_markup=build_model_settings_keyboard(model, user_settings),
        parse_mode="HTML",
    )


async def prompt_for_generation_image(message: Message, *, edit: bool, model: GenerationModel) -> None:
    """Показать шаг загрузки изображения с reply keyboard возврата к настройкам."""
    prompt_text = f"Отправьте изображение для модели {model.title}."
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        "Если передумали, можно вернуться к настройкам.",
        reply_markup=build_back_to_settings_keyboard(),
    )


async def prompt_for_generation_images(message: Message, *, edit: bool, model: GenerationModel) -> None:
    prompt_text = (
        f"Отправьте изображения для модели {model.title}.\n"
        f"Можно загрузить от {model.min_images} до {model.max_images} файлов.\n"
        "После загрузки нажмите ✅ Продолжить."
    )
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        "Если передумали, можно вернуться к настройкам.",
        reply_markup=build_media_upload_reply_keyboard(show_continue=True),
    )


async def prompt_for_generation_video(message: Message, *, edit: bool, model: GenerationModel) -> None:
    """Показать шаг загрузки видео с reply keyboard возврата к настройкам."""
    prompt_text = f"Отправьте видео для модели {model.title}."
    if edit:
        await message.edit_text(prompt_text, reply_markup=None)
    else:
        await message.answer(prompt_text)

    await message.answer(
        "Если передумали, можно вернуться к настройкам.",
        reply_markup=build_back_to_settings_keyboard(),
    )


async def show_setting_options(message: Message, state: FSMContext, setting_key: str) -> None:
    """Показать варианты значения конкретной настройки."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    if not model_key:
        await message.edit_text("❌ Модель не выбрана. Начни заново.", reply_markup=None)
        return
    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await message.edit_text("❌ Настройка не найдена.", reply_markup=None)
        return
    user_settings = get_model_state_settings(state_data, model_key)
    current_value = str(user_settings.get(setting_key, model.user_settings[setting_key].default))
    await message.edit_text(
        build_setting_value_text(model, setting_key, current_value),
        reply_markup=build_setting_options_keyboard(model, setting_key, current_value),
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

    if is_lipsync:
        prompt = get_input_audio_or_text_display(input_audio_or_text)
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
        flow_texts = get_flow_texts(model.generation_type) if model else FLOW_TEXTS["text_to_image"]
        if not model:
            error_text = format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, "модель недоступна.")
        elif is_lipsync:
            error_text = get_lipsync_incomplete_error_text()
        elif not prompt:
            error_text = format_user_error(ErrorCode.E002_MISSING_PROMPT, flow_texts.missing_prompt)
        elif model and model.input_media_field == "images":
            error_text = format_user_error(ErrorCode.E003_MISSING_IMAGE, f"нужно загрузить минимум {model.min_images} изображение.")
        elif required_input_type == "image":
            error_text = format_user_error(ErrorCode.E003_MISSING_IMAGE, flow_texts.missing_media)
        elif required_input_type == "video":
            error_text = format_user_error(ErrorCode.E004_MISSING_VIDEO, flow_texts.missing_media)
        else:
            error_text = format_user_error(ErrorCode.E010_INTERNAL_ERROR, "данные генерации неполные. Начните заново.")
        if edit:
            await message.edit_text(
                error_text,
                reply_markup=None,
            )
        else:
            await message.answer(
                error_text,
                reply_markup=get_main_menu_keyboard(),
            )
        await reset_generation_state(state)
        return

    user_settings = get_model_state_settings(state_data, model_key)
    user_repo = UserRepository(session)
    user = await user_repo.get_or_create_user_from_telegram(telegram_user)
    text = build_confirmation_text(model, user_settings, prompt, user.balance)

    await state.set_state(GenerationStates.waiting_for_confirmation)
    if edit:
        await message.edit_text(text, reply_markup=build_generation_confirm_keyboard(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=build_generation_confirm_keyboard(), parse_mode="HTML")


@router.message(GenerationStates.waiting_for_images, lambda message: message.text == "✅ Продолжить")
async def continue_after_multi_image_upload(message: Message, state: FSMContext):
    """Перейти к prompt после multi-image upload, если набран минимальный набор изображений."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    media_items = get_input_media_items(state_data)
    if not model:
        await message.answer(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, "модель недоступна."))
        return
    if len(media_items) < model.min_images:
        await message.answer(
            format_user_error(ErrorCode.E003_MISSING_IMAGE, f"нужно загрузить минимум {model.min_images} изображение."),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True),
        )
        return
    await state.update_data(input_media={"type": "images", "count": len(media_items)})
    await state.set_state(GenerationStates.waiting_for_prompt)
    await message.answer(get_second_prompt_for_generation_type(model.generation_type), reply_markup=ReplyKeyboardRemove())


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
        await generation_repo.update_generation_status(
            generation_request_id,
            status,
            error_message=error_message,
        )
        if refund_credit:
            refunded = await user_repo.increase_balance(user_id, cost)
            if refunded:
                log_balance_event("balance_refunded", user_id, cost)
        await user_repo.increment_user_generation_stats(user_id, success=False)
    await log_generation_event(generation_request_id, user_id, model_key, status)


async def mark_generation_completed(
    *,
    generation_request_id,
    user_id: int,
    model_key: str,
    nsfw_flags: Optional[dict[str, Any]],
    output_count: int,
) -> None:
    """Обновить статус генерации как completed без сохранения output URLs."""
    async with db_manager.session_factory() as session:
        generation_repo = GenerationRepository(session)
        user_repo = UserRepository(session)
        await generation_repo.update_generation_status(
            generation_request_id,
            "completed",
            nsfw_flags=nsfw_flags,
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
        "⚠️ Файл слишком большой для Telegram.\n\n"
        "Мы загрузили его в защищённое облачное хранилище Cloudflare R2.\n\n"
        f"🔗 Скачать файл:\n{short_url}\n\n"
        "🔒 Ссылка временная и безопасная. Она действует 30 минут.\n\n"
        "Если сомневаетесь, можете проверить ссылку через любой AI, онлайн-анализатор ссылок или открыть её в браузере."
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


async def send_document_with_retry(*, bot, chat_id: int, file_path: str, caption: Optional[str]) -> None:
    """Отправить документ в Telegram c retry при сетевых ошибках."""
    normalized_filename = Path(file_path).name
    for attempt in range(1, DOCUMENT_SEND_RETRY_COUNT + 2):
        try:
            await bot.send_document(
                chat_id,
                FSInputFile(file_path, filename=normalized_filename),
                caption=caption,
                request_timeout=DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS,
            )
            log_generation_output_delivery(
                "telegram",
                file_size_bytes=Path(file_path).stat().st_size,
                status="success",
            )
            return
        except (TelegramNetworkError, TimeoutError):
            if attempt > DOCUMENT_SEND_RETRY_COUNT:
                log_generation_output_delivery(
                    "telegram",
                    file_size_bytes=Path(file_path).stat().st_size,
                    status="failed",
                )
                raise
            await asyncio.sleep(2 ** (attempt - 1))


def get_output_delivery_kind(content_type: Optional[str]) -> str:
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type.startswith("image/"):
        return "photo"
    if normalized_content_type.startswith("video/"):
        return "video"
    return "document"


async def send_photo_output(*, bot, chat_id: int, file_path: str) -> None:
    await bot.send_photo(chat_id, FSInputFile(file_path, filename=Path(file_path).name))


async def send_video_output(*, bot, chat_id: int, file_path: str) -> None:
    await bot.send_video(
        chat_id,
        FSInputFile(file_path, filename=Path(file_path).name),
        request_timeout=DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS,
    )


async def get_user_send_results_as_files(user_id: int) -> bool:
    async with db_manager.session_factory() as session:
        return await UserRepository(session).get_user_delivery_preference(user_id)


async def send_generation_outputs(
    bot,
    chat_id: int,
    output_urls: list[str],
    user_id: Optional[int] = None,
) -> OutputDeliveryResult:
    """Отправить пользователю результаты генерации с учётом пользовательского способа доставки."""
    delivered_successfully = True
    use_r2 = False
    r2_storage = R2StorageService()
    send_results_as_files = False
    if user_id is not None:
        send_results_as_files = await get_user_send_results_as_files(user_id)
    for output_url in output_urls:
        temp_output_path: Optional[str] = None
        content_type: Optional[str] = None
        file_size_bytes: Optional[int] = None
        try:
            temp_output_path, content_type, file_size_bytes = await download_output_file_to_temp(output_url)
            if file_size_bytes is not None and file_size_bytes > get_safe_telegram_document_size_bytes():
                use_r2 = True
                if r2_storage.is_configured():
                    try:
                        short_url = await upload_output_to_r2_and_get_short_url(
                            r2_storage=r2_storage,
                            file_path=temp_output_path,
                            content_type=content_type,
                            file_size_bytes=file_size_bytes,
                        )
                    except Exception:
                        delivered_successfully = False
                        log_generation_output_delivery(
                            "r2",
                            file_size_bytes=file_size_bytes,
                            status="failed",
                        )
                        await safe_send_bot_message(
                            bot,
                            chat_id,
                            "❌ Не удалось загрузить файл. Попробуйте ещё раз позже",
                        )
                        continue
                    await safe_send_bot_message(
                        bot,
                        chat_id,
                        build_large_file_r2_message(short_url),
                    )
                    log_generation_output_delivery(
                        "r2",
                        file_size_bytes=file_size_bytes,
                        status="success",
                    )
                    continue
                delivered_successfully = False
                log_generation_output_delivery(
                    "r2",
                    file_size_bytes=file_size_bytes,
                    status="failed",
                )
                await safe_send_bot_message(
                    bot,
                    chat_id,
                    "❌ Не удалось загрузить файл. Попробуйте ещё раз позже",
                )
                continue
            delivery_kind = "document" if send_results_as_files else get_output_delivery_kind(content_type)
            if delivery_kind == "photo":
                try:
                    await send_photo_output(bot=bot, chat_id=chat_id, file_path=temp_output_path)
                    log_generation_output_delivery(
                        "telegram_photo",
                        file_size_bytes=file_size_bytes,
                        status="success",
                    )
                    continue
                except Exception:
                    logger.exception("Failed to deliver completed Wavespeed output as photo")
            elif delivery_kind == "video":
                try:
                    await send_video_output(bot=bot, chat_id=chat_id, file_path=temp_output_path)
                    log_generation_output_delivery(
                        "telegram_video",
                        file_size_bytes=file_size_bytes,
                        status="success",
                    )
                    continue
                except Exception:
                    logger.exception("Failed to deliver completed Wavespeed output as video")

            await send_document_with_retry(
                bot=bot,
                chat_id=chat_id,
                file_path=temp_output_path,
                caption=None,
            )
            log_generation_output_delivery(
                "telegram_document",
                file_size_bytes=file_size_bytes,
                status="success",
            )
        except OutputDeliveryTooLargeError:
            delivered_successfully = False
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
                    log_generation_output_delivery(
                        "r2",
                        file_size_bytes=file_size_bytes,
                        status="failed",
                    )
                    await safe_send_bot_message(
                        bot,
                        chat_id,
                        "❌ Не удалось загрузить файл. Попробуйте ещё раз позже",
                    )
                    continue
                await safe_send_bot_message(bot, chat_id, build_large_file_r2_message(short_url))
                log_generation_output_delivery(
                    "r2",
                    file_size_bytes=file_size_bytes,
                    status="success",
                )
                delivered_successfully = True
                continue
            log_generation_output_delivery(
                "r2",
                file_size_bytes=file_size_bytes,
                status="failed",
            )
            await safe_send_bot_message(
                bot,
                chat_id,
                "❌ Не удалось загрузить файл. Попробуйте ещё раз позже",
            )
        except Exception:
            logger.exception("Failed to deliver completed Wavespeed output as document")
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
                    delivered_successfully = False
                    log_generation_output_delivery(
                        "r2",
                        file_size_bytes=file_size_bytes,
                        status="failed",
                    )
                    await safe_send_bot_message(
                        bot,
                        chat_id,
                        "❌ Не удалось загрузить файл. Попробуйте ещё раз позже",
                    )
                    continue
                log_generation_output_delivery(
                    "r2",
                    file_size_bytes=file_size_bytes,
                    status="success",
                )
                await safe_send_bot_message(
                    bot,
                    chat_id,
                    build_large_file_r2_message(short_url),
                )
                continue
            delivered_successfully = False
            log_generation_output_delivery(
                "telegram",
                file_size_bytes=file_size_bytes,
                status="failed",
            )
            await safe_send_bot_message(
                bot,
                chat_id,
                "Файл готов, но Telegram не смог его доставить",
            )
        finally:
            await cleanup_temp_output_file(temp_output_path)
    return OutputDeliveryResult(delivered_successfully=delivered_successfully, use_r2=use_r2)


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
    file_size_bytes: Optional[int] = None,
    status: str,
) -> None:
    """Логировать только безопасные метаданные доставки результатов генерации."""
    logger.info(
        {
            "action": "generation_output_delivery",
            "method": delivery_method,
            "file_size": file_size_bytes,
            "status": status,
        }
    )


def get_output_suffix_and_type(content_type: Optional[str]) -> str:
    """Определить расширение временного output-файла по Content-Type."""
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized_content_type == "image/png":
        return ".png"
    if normalized_content_type == "image/jpeg":
        return ".jpg"
    if normalized_content_type == "image/webp":
        return ".webp"
    if normalized_content_type == "video/mp4":
        return ".mp4"
    return ".bin"


def get_content_type_for_path(file_path: str) -> Optional[str]:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".jpg":
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".mp4":
        return "video/mp4"
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


async def poll_generation_result(
    *,
    bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    generation_request_id,
    model_key: str,
    cost: int,
    payload: dict[str, Any],
    temp_input_path: Optional[str] | list[str],
) -> None:
    """Выполнить submit и дождаться terminal результата, затем удалить временный input media."""
    await _run_single_generation_request(
        bot=bot,
        state=state,
        user_id=user_id,
        chat_id=chat_id,
        generation_request_id=generation_request_id,
        model_key=model_key,
        cost=cost,
        payload=payload,
        temp_input_path=temp_input_path,
        clear_active_generation=True,
        cleanup_inputs=True,
        reset_state_after=True,
        use_partial_failure_message=False,
    )


async def _run_single_generation_request(
    *,
    bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    generation_request_id,
    model_key: str,
    cost: int,
    payload: dict[str, Any],
    temp_input_path: Optional[str] | list[str],
    clear_active_generation: bool,
    cleanup_inputs: bool,
    reset_state_after: bool,
    use_partial_failure_message: bool,
) -> None:
    wavespeed = WavespeedService()
    result: Optional[WavespeedResult] = None
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

        result = await wavespeed.poll_until_complete(
            prediction_id,
            timeout_seconds=POLL_TIMEOUT_SECONDS,
            interval=60,
        )
        await mark_generation_completed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            nsfw_flags=result.raw_response.get("nsfw_flags"),
            output_count=len(result.outputs),
        )
        await send_generation_outputs(bot, chat_id, result.outputs, user_id)
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
        await safe_send_bot_message(
            bot,
            chat_id,
            build_partial_generation_failed_message() if use_partial_failure_message else get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
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
            error_message=safe_error_message or "Генерация завершилась с ошибкой.",
            refund_credit=True,
        )
        await safe_send_bot_message(
            bot,
            chat_id,
            build_partial_generation_failed_message() if use_partial_failure_message else get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
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
        await safe_send_bot_message(
            bot,
            chat_id,
            build_partial_generation_failed_message() if use_partial_failure_message else get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
        )
    except Exception as exc:
        logger.exception("Error while polling generation result")
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
            error_message="Не удалось завершить генерацию. Попробуйте позже.",
            refund_credit=True,
        )
        await safe_send_bot_message(
            bot,
            chat_id,
            build_partial_generation_failed_message() if use_partial_failure_message else get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
        )
    finally:
        if clear_active_generation:
            ACTIVE_GENERATIONS.pop(user_id, None)
        if cleanup_inputs:
            await cleanup_generation_file(temp_input_path)
        if reset_state_after:
            await reset_generation_state(state)
        await wavespeed.close()


async def poll_generation_results_batch(
    *,
    bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    generation_request_ids: list[Any],
    model_key: str,
    payload: dict[str, Any],
    temp_input_path: Optional[str] | list[str],
) -> None:
    semaphore = asyncio.Semaphore(4)

    async def _run_child(generation_request_id) -> None:
        async with semaphore:
            await _run_single_generation_request(
                bot=bot,
                state=state,
                user_id=user_id,
                chat_id=chat_id,
                generation_request_id=generation_request_id,
                model_key=model_key,
                cost=GENERATION_COST,
                payload=dict(payload),
                temp_input_path=None,
                clear_active_generation=False,
                cleanup_inputs=False,
                reset_state_after=False,
                use_partial_failure_message=True,
            )

    try:
        results = await asyncio.gather(
            *(_run_child(generation_request_id) for generation_request_id in generation_request_ids),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.exception("Batch generation task failed unexpectedly: %s", result)
    finally:
        ACTIVE_GENERATIONS.pop(user_id, None)
        await cleanup_generation_file(temp_input_path)
        await reset_generation_state(state)


@router.message(lambda msg: msg.text == "🎨 Генерации")
async def show_generation_menu(message: Message, state: FSMContext):
    """Показать меню генерации."""
    try:
        current_state = await state.get_state()
        if current_state is not None or message.from_user.id in ACTIVE_GENERATIONS:
            await message.answer(
                "⚠️ У вас уже есть активный сценарий генерации. Дождитесь завершения или вернитесь в главное меню через /start.",
                reply_markup=get_main_menu_keyboard(),
            )
            return

        await reset_generation_state(state)
        await state.set_state(GenerationStates.choosing_generation_type)
        await state.update_data(selected_generation_type=None, selected_provider=None)
        await render_models_screen(message)
        
        logger.debug(f"Generation menu shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_generation_menu: %s", e)
        await message.answer("❌ Произошла ошибка при открытии меню генерации")


@router.callback_query(lambda cb: cb.data.startswith(MODEL_PREFIX))
async def choose_generation_model(callback: CallbackQuery, state: FSMContext):
    """Выбрать модель для генерации."""
    log_generation_callback(callback)
    model_token = callback.data.removeprefix(MODEL_PREFIX)
    if callback.from_user.id in ACTIVE_GENERATIONS:
        await callback.answer("У вас уже запущена генерация", show_alert=True)
        return

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
        await callback.message.edit_text(
            "В этом разделе пока нет подключённых моделей",
            reply_markup=build_models_keyboard([], BACK_TO_SECTIONS),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await render_model_list_screen(
        callback.message,
        models=models,
        edit=True,
        heading="Выберите модель:",
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
    await callback.message.edit_text(
        build_generation_types_screen_text(),
        reply_markup=build_generation_sections_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(PROVIDER_PREFIX))
async def choose_provider(callback: CallbackQuery, state: FSMContext):
    """Выбрать провайдера и показать его модели."""
    log_generation_callback(callback)
    provider = callback.data.removeprefix(PROVIDER_PREFIX)
    if provider not in list_providers():
        await callback.answer("Провайдер недоступен", show_alert=True)
        return
    models = list_models_by_provider(provider)
    if not models:
        await state.set_state(GenerationStates.choosing_provider)
        await state.update_data(selected_generation_type="all", selected_provider=None)
        await callback.message.edit_text(
            "У этого провайдера пока нет подключённых моделей",
            reply_markup=build_providers_keyboard(),
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
        heading="Выберите модель:",
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
            heading="Выберите модель:",
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
            heading="Выберите модель:",
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
    setting_key = callback.data.removeprefix(SETTINGS_OPEN_PREFIX)
    if not setting_key:
        await callback.answer("Настройка не найдена", show_alert=True)
        return
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    if not model_key:
        await callback.message.edit_text("❌ Модель не выбрана. Начни заново.", reply_markup=None)
        await callback.answer()
        return
    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await callback.answer("Настройка не найдена", show_alert=True)
        return
    setting = model.user_settings[setting_key]
    await state.update_data(current_setting_key=setting_key)
    if setting.type == "text":
        user_settings = get_model_state_settings(state_data, model_key)
        current_value = str(user_settings.get(setting_key, setting.default))
        await state.set_state(GenerationStates.waiting_for_setting_text)
        await callback.message.edit_text(
            build_setting_value_text(model, setting_key, current_value),
            reply_markup=None,
            parse_mode="HTML",
        )
        await callback.message.answer(
            "Если передумали, можно вернуться к настройкам.",
            reply_markup=build_back_to_settings_keyboard(),
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


@router.message(GenerationStates.waiting_for_setting_text, lambda message: message.text == "⬅️ Назад к настройкам")
async def back_to_settings_from_text_setting(message: Message, state: FSMContext):
    """Вернуться с текстового ввода настройки к экрану настроек модели."""
    await state.set_state(GenerationStates.choosing_settings)
    await state.update_data(current_setting_key=None)
    await message.answer("Возвращаю к настройкам модели.", reply_markup=ReplyKeyboardRemove())
    await render_settings_screen_message(message, state, edit=False)


@router.message(GenerationStates.waiting_for_setting_text)
async def process_text_setting_value(message: Message, state: FSMContext):
    """Сохранить текстовое значение настройки и вернуть пользователя к настройкам модели."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    setting_key = state_data.get("current_setting_key")
    if not model_key or not setting_key:
        await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, "настройка не выбрана."))
        return

    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, "настройка недоступна."))
        return

    if message_contains_file(message):
        await message.answer(
            format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, "нужно отправить текстовое значение настройки."),
            reply_markup=build_back_to_settings_keyboard(),
        )
        return

    raw_text = (message.text or "").strip()
    value = "" if raw_text in {"", "-"} else raw_text
    user_settings = get_model_state_settings(state_data, model_key)
    user_settings[str(setting_key)] = value
    await state.update_data(user_settings=user_settings, current_setting_key=None)
    await state.set_state(GenerationStates.choosing_settings)
    await message.answer("Значение сохранено.", reply_markup=ReplyKeyboardRemove())
    await render_settings_screen_message(message, state, edit=False)


@router.callback_query(lambda cb: cb.data.startswith(SETTINGS_VALUE_PREFIX))
async def choose_setting_value(callback: CallbackQuery, state: FSMContext):
    """Сохранить выбранное значение настройки и вернуться к экрану настроек."""
    log_generation_callback(callback)
    setting_payload = callback.data.removeprefix(SETTINGS_VALUE_PREFIX)
    if ":" not in setting_payload:
        await callback.answer("Некорректное значение", show_alert=True)
        return
    setting_key, option_index_raw = setting_payload.rsplit(":", 1)
    if not option_index_raw.isdigit():
        await callback.answer("Некорректное значение", show_alert=True)
        return
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    if not model_key:
        await callback.message.edit_text("❌ Модель не выбрана. Начни заново.", reply_markup=None)
        await callback.answer()
        return

    model = get_generation_model(model_key)
    if setting_key not in model.user_settings:
        await callback.answer("Настройка не найдена", show_alert=True)
        return

    user_settings = get_model_state_settings(state_data, model_key)
    option_index = int(option_index_raw)
    options = model.user_settings[setting_key].options
    if option_index < 0 or option_index >= len(options):
        await callback.answer("Некорректное значение", show_alert=True)
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
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    if not model:
        if state_data.get("model_generation_type") == "lipsync":
            await state.set_state(GenerationStates.waiting_for_image)
            await prompt_for_generation_input(callback.message, edit=True, is_lipsync=True)
            await callback.answer()
            return
        await callback.message.edit_text(
            format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, "модель недоступна."),
            reply_markup=None,
        )
        await callback.answer()
        return

    required_input_type = get_required_input_type(model.generation_type)
    if required_input_type == "text":
        await state.set_state(GenerationStates.waiting_for_prompt)
        await callback.message.edit_text(get_prompt_for_generation_type(model.generation_type), reply_markup=None)
        await callback.message.answer(
            "Если передумали, можно вернуться к настройкам.",
            reply_markup=build_back_to_settings_keyboard(),
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


@router.message(GenerationStates.waiting_for_images, lambda message: message.text == "⬅️ Назад к настройкам")
@router.message(GenerationStates.waiting_for_image, lambda message: message.text == "⬅️ Назад к настройкам")
@router.message(GenerationStates.waiting_for_video, lambda message: message.text == "⬅️ Назад к настройкам")
async def back_to_settings_from_image_step(message: Message, state: FSMContext):
    """Вернуться с этапа загрузки изображения к настройкам модели без потери выбранных значений."""
    await cleanup_state_media(state)
    await state.set_state(GenerationStates.choosing_settings)
    await message.answer("Возвращаю к настройкам модели.", reply_markup=ReplyKeyboardRemove())
    await render_settings_screen_message(message, state, edit=False)


@router.message(GenerationStates.waiting_for_image, lambda message: bool(message.photo) or bool(message.document) or bool(message.video))
async def process_generation_image(message: Message, state: FSMContext):
    """Принять изображение для генерации."""
    state_data = await state.get_data()
    is_lipsync = is_lipsync_generation_state(state_data)
    model_generation_type = str(state_data.get("model_generation_type") or "image_edit")
    if is_lipsync:
        document = message.document
        photo = message.photo[-1] if message.photo else None
        video = message.video

        if document and not is_supported_media_document(document, is_lipsync=True):
            await message.answer(
                "❌ Нужны фото лица или видео. Отправь фото, video или document с форматом image/* или video/*.",
                reply_markup=build_back_to_settings_keyboard(),
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
        await state.set_state(GenerationStates.waiting_for_prompt)
        await message.answer(
            get_second_step_prompt_text(is_lipsync=True),
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    received_type = None
    if message.video or (message.document and extract_document_media_type(message.document) == "video"):
        received_type = "video"
    if not is_supported_image_input(message):
        await message.answer(
            build_invalid_input_message("image", model_generation_type, received_type=received_type),
            reply_markup=build_back_to_settings_keyboard(),
        )
        return

    try:
        media_item = await upload_message_media_item(message)
    except ImageUploadError:
        log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=message.from_user.id, status="failed")
        await message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, "не удалось подготовить изображение."),
            reply_markup=build_back_to_settings_keyboard(),
        )
        return

    await state.update_data(
        input_media={"type": media_item["type"], "count": 1},
        input_media_items=[media_item],
        input_image_file_id=media_item.get("file_id"),
    )
    await state.set_state(GenerationStates.waiting_for_prompt)
    await message.answer(
        get_second_prompt_for_generation_type(model_generation_type),
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(GenerationStates.waiting_for_images, lambda message: bool(message.photo) or bool(message.document) or bool(message.video))
async def process_generation_images(message: Message, state: FSMContext):
    """Принять очередное изображение для multi-image модели."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    model_generation_type = str(state_data.get("model_generation_type") or "image_edit")
    received_type = None
    if message.video or (message.document and extract_document_media_type(message.document) == "video"):
        received_type = "video"
    if not model:
        await message.answer(format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, "модель недоступна."))
        return
    if not is_supported_image_input(message):
        await message.answer(
            build_invalid_input_message("image", model_generation_type, received_type=received_type),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True),
        )
        return

    media_items = get_input_media_items(state_data)
    if len(media_items) >= model.max_images:
        await message.answer(
            f"Достигнут лимит {model.max_images} изображений.",
            reply_markup=build_media_upload_reply_keyboard(show_continue=True),
        )
        return

    try:
        media_item = await upload_message_media_item(message)
    except ImageUploadError:
        log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=message.from_user.id, status="failed")
        await message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, "не удалось подготовить изображение."),
            reply_markup=build_media_upload_reply_keyboard(show_continue=True),
        )
        return

    media_items.append(media_item)
    await state.update_data(
        input_media_items=media_items,
        input_media={"type": "images", "count": len(media_items)},
        input_image_file_id=media_items[0].get("file_id"),
    )
    if len(media_items) >= model.max_images:
        await state.set_state(GenerationStates.waiting_for_prompt)
        await message.answer(
            get_second_prompt_for_generation_type(model_generation_type),
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await message.answer(
        f"Загружено {len(media_items)} из {model.max_images}. Отправьте ещё изображение или нажмите ✅ Продолжить.",
        reply_markup=build_media_upload_reply_keyboard(show_continue=True),
    )


@router.message(GenerationStates.waiting_for_image)
async def invalid_generation_image(message: Message, state: FSMContext):
    """Сообщить, что ожидается изображение."""
    state_data = await state.get_data()
    is_lipsync = is_lipsync_generation_state(state_data)
    await message.answer(
        "❌ Я жду фото лица или видео. Отправь фото, video или document с форматом image/* или video/*."
        if is_lipsync
        else "❌ Я жду изображение. Отправь фото Telegram или document с форматом image/*.",
        reply_markup=build_back_to_settings_keyboard(),
    )


@router.message(GenerationStates.waiting_for_images)
async def invalid_generation_images(message: Message, state: FSMContext):
    """Сообщить, что ожидается изображение для multi-image flow."""
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    model = get_generation_model(model_key) if model_key else None
    max_images = model.max_images if model else 1
    await message.answer(
        format_user_error(ErrorCode.E001_INVALID_INPUT_TYPE, "нужно отправить изображение."),
        reply_markup=build_media_upload_reply_keyboard(show_continue=True if max_images else False),
    )


@router.message(GenerationStates.waiting_for_video, lambda message: bool(message.photo) or bool(message.document) or bool(message.video))
async def process_generation_video(message: Message, state: FSMContext):
    """Принять видео для генерации."""
    state_data = await state.get_data()
    model_generation_type = str(state_data.get("model_generation_type") or "video_edit")
    received_type = None
    if message.photo or (message.document and extract_document_media_type(message.document) == "image"):
        received_type = "image"

    if not is_supported_video_input(message):
        await message.answer(
            build_invalid_input_message("video", model_generation_type, received_type=received_type),
            reply_markup=build_back_to_settings_keyboard(),
        )
        return

    try:
        media_item = await upload_message_media_item(message)
    except ImageUploadError:
        log_generation_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, user_id=message.from_user.id, status="failed")
        await message.answer(
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, "не удалось подготовить видео."),
            reply_markup=build_back_to_settings_keyboard(),
        )
        return

    await state.update_data(
        input_media={"type": media_item["type"], "count": 1},
        input_media_items=[media_item],
        input_image_file_id=media_item.get("file_id"),
    )
    await state.set_state(GenerationStates.waiting_for_prompt)
    await message.answer(
        get_second_prompt_for_generation_type(model_generation_type),
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(GenerationStates.waiting_for_video)
async def invalid_generation_video(message: Message, state: FSMContext):
    """Сообщить, что ожидается видео."""
    state_data = await state.get_data()
    model_generation_type = str(state_data.get("model_generation_type") or "video_edit")
    await message.answer(
        build_invalid_input_message("video", model_generation_type),
        reply_markup=build_back_to_settings_keyboard(),
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
        model_key = state_data.get("model_key", "nano_banana")
        model = get_generation_model(model_key)
        is_lipsync = is_lipsync_generation_state(state_data)
        required_input_type = get_required_input_type(model.generation_type)
        input_media = state_data.get("input_media")
        input_media_items = get_input_media_items(state_data)
        input_audio_or_text = None
        prompt = ""
        
        if is_lipsync:
            input_audio_or_text = build_input_audio_or_text_payload(message)
            prompt = get_input_audio_or_text_display(input_audio_or_text)
            if not input_audio_or_text:
                await message.answer("❌ Отправь текст, голосовое сообщение или аудиофайл для озвучки.")
                return
            if not input_media:
                await message.answer("❌ Сначала отправь фото лица или видео.", reply_markup=build_back_to_settings_keyboard())
                await state.set_state(GenerationStates.waiting_for_image)
                return
        else:
            if message_contains_file(message):
                await message.answer(
                    build_invalid_input_message("text", model.generation_type),
                    reply_markup=build_back_to_settings_keyboard(),
                )
                return
            prompt = (message.text or "").strip()
            if not prompt:
                await message.answer(
                    format_user_error(ErrorCode.E002_MISSING_PROMPT, get_flow_texts(model.generation_type).missing_prompt),
                    reply_markup=build_back_to_settings_keyboard(),
                )
                return
            if len(prompt) < 10:
                await message.answer(
                    format_user_error(ErrorCode.E002_MISSING_PROMPT, "описание слишком короткое."),
                    reply_markup=build_back_to_settings_keyboard(),
                )
                return
            if model.input_media_field == "images" and len(input_media_items) < model.min_images and not input_media:
                await message.answer(
                    format_user_error(ErrorCode.E003_MISSING_IMAGE, f"нужно загрузить минимум {model.min_images} изображение."),
                    reply_markup=build_media_upload_reply_keyboard(show_continue=True),
                )
                await state.set_state(GenerationStates.waiting_for_images)
                return
            if required_input_type == "image" and not input_media:
                await message.answer(
                    format_user_error(ErrorCode.E003_MISSING_IMAGE, get_flow_texts(model.generation_type).missing_media),
                    reply_markup=build_back_to_settings_keyboard(),
                )
                await state.set_state(GenerationStates.waiting_for_image)
                return
            if required_input_type == "video" and not input_media:
                await message.answer(
                    format_user_error(ErrorCode.E004_MISSING_VIDEO, get_flow_texts(model.generation_type).missing_media),
                    reply_markup=build_back_to_settings_keyboard(),
                )
                await state.set_state(GenerationStates.waiting_for_video)
                return
        
        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        
        if not user:
            await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, "пользователь не найден."))
            return
        
        user_settings = get_model_state_settings(state_data, model_key)
        total_cost = get_total_generation_cost(model, user_settings)
        if not await user_repo.has_enough_balance(user.id, total_cost):
            log_generation_error(ErrorCode.E006_INSUFFICIENT_BALANCE, user_id=user.id, model_key=model_key, status="rejected")
            await message.answer(
                format_user_error(
                    ErrorCode.E006_INSUFFICIENT_BALANCE,
                    f"Недостаточно кредитов. Нужно {total_cost}, у вас {user.balance}.",
                )
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
        await message.answer(format_user_error(ErrorCode.E010_INTERNAL_ERROR, "ошибка при обработке запроса."))


@router.callback_query(lambda cb: cb.data == GENERATION_CONFIRM)
async def confirm_generation(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Запустить генерацию после подтверждения."""
    log_generation_callback(callback)
    user_id = callback.from_user.id
    if user_id in ACTIVE_GENERATIONS:
        await callback.answer("Генерация уже запущена", show_alert=True)
        return

    state_data = await state.get_data()
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
    if not input_media and input_image_file_id:
        legacy_media_type = "image" if required_input_type != "video" else "video"
        input_media = {"type": legacy_media_type, "file_id": input_image_file_id}
    try:
        user_settings = validate_model_settings(model_key, state_data.get("user_settings")) if model_key else {}
    except ValueError:
        log_generation_error(ErrorCode.E011_INVALID_MODEL_SETTINGS, user_id=user_id, model_key=model_key, status="rejected")
        await callback.message.answer(
            format_user_error(ErrorCode.E011_INVALID_MODEL_SETTINGS, "некорректные настройки модели."),
            reply_markup=get_main_menu_keyboard(),
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
            error_text = format_user_error(ErrorCode.E005_UNSUPPORTED_MODEL, "модель недоступна.")
        elif is_lipsync:
            error_text = get_lipsync_incomplete_error_text()
        elif not prompt:
            error_text = format_user_error(ErrorCode.E002_MISSING_PROMPT, get_flow_texts(model.generation_type).missing_prompt)
        elif model and model.input_media_field == "images":
            error_text = format_user_error(ErrorCode.E003_MISSING_IMAGE, f"нужно загрузить минимум {model.min_images} изображение.")
        elif required_input_type == "image":
            error_text = format_user_error(ErrorCode.E003_MISSING_IMAGE, get_flow_texts(model.generation_type).missing_media)
        elif required_input_type == "video":
            error_text = format_user_error(ErrorCode.E004_MISSING_VIDEO, get_flow_texts(model.generation_type).missing_media)
        else:
            error_text = format_user_error(ErrorCode.E010_INTERNAL_ERROR, "данные генерации неполные. Начните заново.")
        await callback.message.answer(
            error_text,
            reply_markup=get_main_menu_keyboard(),
        )
        await reset_generation_state(state)
        await callback.answer()
        return

    debited_balance = False
    debited_user_id: Optional[int] = None
    generation_request_id = None
    generation_request_ids: list[Any] = []
    temp_input_path: Optional[str] | list[str] = None
    task_started = False
    total_cost = GENERATION_COST

    try:
        user_repo = UserRepository(session)
        generation_repo = GenerationRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
        debited_user_id = user.id
        num_generations = get_model_num_generations(model, user_settings)
        total_cost = get_total_generation_cost(model, user_settings)

        if not await user_repo.decrease_balance(user.id, total_cost):
            log_balance_event("insufficient_balance", user.id, total_cost)
            log_generation_error(ErrorCode.E006_INSUFFICIENT_BALANCE, user_id=user.id, model_key=model_key, status="rejected")
            await callback.message.answer(
                format_user_error(
                    ErrorCode.E006_INSUFFICIENT_BALANCE,
                    f"Недостаточно кредитов. Нужно {total_cost}, у вас {user.balance}.",
                ),
                reply_markup=get_main_menu_keyboard(),
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

        await state.set_state(GenerationStates.generating)
        if num_generations == 1:
            task = asyncio.create_task(
                poll_generation_result(
                    bot=callback.bot,
                    state=state,
                    user_id=user_id,
                    chat_id=callback.message.chat.id,
                    generation_request_id=generation_request_id,
                    model_key=model_key,
                    cost=GENERATION_COST,
                    payload=payload,
                    temp_input_path=temp_input_path,
                )
            )
        else:
            task = asyncio.create_task(
                poll_generation_results_batch(
                    bot=callback.bot,
                    state=state,
                    user_id=user_id,
                    chat_id=callback.message.chat.id,
                    generation_request_ids=generation_request_ids,
                    model_key=model_key,
                    payload=payload,
                    temp_input_path=temp_input_path,
                )
            )
        task.add_done_callback(log_background_task_exception)
        task_started = True
        ACTIVE_GENERATIONS[user_id] = {
            "task": task,
            "generation_request_id": generation_request_id,
            "generation_request_ids": generation_request_ids,
        }

        await callback.message.edit_text(
            (
                f"Запущено генераций: {num_generations}. Результаты будут приходить по мере готовности.\n\n"
                f"Модель: <b>{escape(model_title)}</b>"
            ),
            parse_mode="HTML",
        )
        await callback.message.answer(
            f"Запущено генераций: {num_generations}. Результаты будут приходить по мере готовности.",
            reply_markup=get_main_menu_keyboard(),
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
            format_user_error(ErrorCode.E012_MEDIA_UPLOAD_FAILED, "не удалось подготовить медиа для генерации."),
            reply_markup=get_main_menu_keyboard(),
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
            format_user_error(ErrorCode.E011_INVALID_MODEL_SETTINGS, "некорректные настройки модели."),
            reply_markup=get_main_menu_keyboard(),
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
                    error_message="Не удалось запустить генерацию. Попробуйте позже.",
                )
        if debited_balance and debited_user_id is not None:
            try:
                user_repo = UserRepository(session)
                await user_repo.increase_balance(debited_user_id, total_cost)
            except Exception as refund_exc:
                logger.exception("Error while refunding balance after launch failure: %s", refund_exc)
        await state.clear()
        await callback.message.answer(
            format_user_error(ErrorCode.E010_INTERNAL_ERROR, "не удалось запустить генерацию."),
            reply_markup=get_main_menu_keyboard(),
        )
        await callback.answer()
@router.callback_query(F.data.startswith("gen:"))
async def handle_unknown_generation_callback(callback: CallbackQuery, state: FSMContext):
    """Fallback для устаревших или неподдерживаемых inline-кнопок генераций."""
    log_generation_callback(callback)
    await state.set_state(GenerationStates.choosing_generation_type)
    await state.update_data(selected_generation_type=None, selected_provider=None)
    await callback.answer("Кнопка устарела. Откройте Генерации заново.", show_alert=True)
    await callback.message.edit_text(
        build_generation_types_screen_text(),
        reply_markup=build_generation_sections_keyboard(),
        parse_mode="HTML",
    )
