"""Роутер генерации контента."""
import asyncio
from html import escape
from pathlib import Path
import tempfile
from typing import Any, Dict, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message, ReplyKeyboardRemove
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import (
    build_back_to_settings_keyboard,
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
from app.db import GenerationRepository, UserRepository
from app.db.session import db_manager
from app.services.generation_service import (
    GenerationModel,
    build_payload,
    get_default_settings,
    get_generation_model,
    list_generation_types,
    list_generation_models,
    list_models_by_provider,
    list_models_by_type,
    validate_model_settings,
    list_providers,
)
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
DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS = 180
DOCUMENT_SEND_RETRY_COUNT = 2
DOCUMENT_SEND_RETRY_DELAY_SECONDS = 5
MAX_TELEGRAM_DOCUMENT_SIZE_BYTES = 50 * 1024 * 1024

MODEL_PREFIX = "gen:model:"
GENERATION_SECTION_PREFIX = "gen:section:"
GENERATION_ALL = "gen:all"
PROVIDER_PREFIX = "gen:provider:"
SETTINGS_OPEN_PREFIX = "gen:setting:"
SETTINGS_VALUE_PREFIX = "gen:set:"
BACK_TO_MAIN = "gen:back:main"
BACK_TO_SECTIONS = "gen:back:sections"
BACK_TO_PROVIDERS = "gen:back:providers"
SETTINGS_BACK_PREFIX = "gen:back:settings"
SETTINGS_BACK_MODELS = "gen:back:models"
SETTINGS_CONTINUE = "gen:continue"
GENERATION_CONFIRM = "gen:confirm"

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
    "midjourney": "Midjourney",
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
        return (
            f"Настройки модели: <b>{escape(model.title)}</b>\n\n"
            f"Параметр: <b>{escape(setting.title)}</b>\n"
            f"Текущее значение: <code>{escape(current_value)}</code>\n\n"
            "Для текстовых параметров сейчас используется сохранённое значение по умолчанию."
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
    balance_after_launch = max(balance - GENERATION_COST, 0)
    prompt_label = "Prompt"
    if model.generation_type == "lipsync":
        prompt_label = "Озвучка"
    return (
        f"Проверьте генерацию:\n\n"
        f"Модель: <b>{escape(model.title)}</b>\n"
        f"Настройки:\n{format_generation_settings(model, user_settings)}\n\n"
        f"{prompt_label}: <i>{escape(prompt)}</i>\n\n"
        f"Стоимость: 1 кредит\n"
        f"Баланс после запуска: <code>{balance_after_launch}</code>"
    )


def get_user_friendly_error_message(error: Exception, result: Optional[WavespeedResult] = None) -> str:
    """Вернуть безопасное и понятное сообщение об ошибке для пользователя."""
    if isinstance(error, WavespeedTimeoutError):
        return "⏱ Генерация заняла слишком много времени и была остановлена. Кредит возвращён."

    if isinstance(error, WavespeedFailedError) or (result is not None and result.status == "failed"):
        safe_error_message = None
        if result is not None:
            safe_error_message = sanitize_external_error_message(result.error)
        if not safe_error_message and isinstance(error, WavespeedFailedError):
            safe_error_message = sanitize_external_error_message(error.user_message)
        if safe_error_message:
            return f"❌ Генерация не удалась. Кредит возвращён.\n\nПричина: {safe_error_message}"
        return "❌ Генерация не удалась. Кредит возвращён. Попробуйте другое изображение, описание или размер."

    if isinstance(error, TelegramBadRequest):
        return "⚠️ Генерация выполнена, но Telegram не смог доставить результат. Попробуйте позже."

    if isinstance(error, (WavespeedNetworkError, httpx.HTTPError, httpx.TimeoutException, TimeoutError)):
        return "🌐 Ошибка сети при получении результата. Попробуйте позже."

    return "⚠️ Произошла неизвестная ошибка. Попробуйте ещё раз."


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
        return (
            "Вы выбрали Lipsync.\n"
            "Отправьте фото или видео, затем текст или голос для озвучки."
        )
    return "Отправьте изображение как фото или файлом."


def get_lipsync_incomplete_error_text() -> str:
    """Вернуть единое сообщение о неполных входных данных lipsync."""
    return "❌ Для lipsync нужно изображение/видео и текст или аудио."


def get_second_step_prompt_text(*, is_lipsync: bool) -> str:
    """Вернуть текст второго шага после загрузки media."""
    if is_lipsync:
        return "Теперь отправьте текст или голосовое сообщение для озвучки."
    return "Опишите, что нужно изменить на изображении."


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


async def prompt_for_generation_image(message: Message, *, edit: bool) -> None:
    """Показать шаг загрузки изображения с reply keyboard возврата к настройкам."""
    await prompt_for_generation_input(message, edit=edit, is_lipsync=False)


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
    prompt = (state_data.get("prompt") or "").strip()
    input_image_file_id = state_data.get("input_image_file_id")
    input_media = state_data.get("input_media")
    input_audio_or_text = state_data.get("input_audio_or_text")

    if is_lipsync:
        prompt = get_input_audio_or_text_display(input_audio_or_text)
        is_complete = bool(model_key and input_media and input_audio_or_text)
    else:
        is_complete = bool(model_key and prompt and input_image_file_id)

    if not is_complete:
        if edit:
            await message.edit_text(
                get_lipsync_incomplete_error_text() if is_lipsync else "❌ Данные генерации неполные. Начни заново.",
                reply_markup=None,
            )
        else:
            await message.answer(
                get_lipsync_incomplete_error_text() if is_lipsync else "❌ Данные генерации неполные. Начни заново.",
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
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as exc:
        logger.exception("Failed to send Telegram message to user: %s", type(exc).__name__)


async def send_document_with_retry(*, bot, chat_id: int, file_path: str, caption: Optional[str]) -> None:
    """Отправить документ в Telegram c retry при сетевых ошибках."""
    for attempt in range(DOCUMENT_SEND_RETRY_COUNT + 1):
        try:
            await bot.send_document(
                chat_id,
                FSInputFile(file_path),
                caption=caption,
                request_timeout=DOCUMENT_SEND_REQUEST_TIMEOUT_SECONDS,
            )
            return
        except TelegramNetworkError:
            if attempt >= DOCUMENT_SEND_RETRY_COUNT:
                raise
            await asyncio.sleep(DOCUMENT_SEND_RETRY_DELAY_SECONDS)


async def send_generation_outputs(bot, chat_id: int, output_urls: list[str]) -> bool:
    """Отправить пользователю результаты генерации только как document через временный локальный файл."""
    delivered_successfully = True
    for index, output_url in enumerate(output_urls, start=1):
        caption = "Готово ✅" if index == 1 else None
        temp_output_path: Optional[str] = None
        try:
            temp_output_path, content_type, file_size_bytes = await download_output_file_to_temp(output_url)
            await send_document_with_retry(
                bot=bot,
                chat_id=chat_id,
                file_path=temp_output_path,
                caption=caption,
            )
            log_generation_output_delivery(
                len(output_urls),
                "downloaded_document",
                content_type=content_type,
                file_size_bytes=file_size_bytes,
            )
        except OutputDeliveryTooLargeError:
            delivered_successfully = False
            log_generation_output_delivery(len(output_urls), "delivery_failed")
            await safe_send_bot_message(
                bot,
                chat_id,
                "⚠️ Файл получился слишком большим для отправки в Telegram.",
            )
        except Exception as exc:
            delivered_successfully = False
            logger.exception(
                "Failed to deliver completed Wavespeed output as document: %s",
                type(exc).__name__,
            )
            log_generation_output_delivery(len(output_urls), "delivery_failed")
            await safe_send_bot_message(
                bot,
                chat_id,
                "⚠️ Генерация завершена, но Telegram не смог доставить файл. Попробуйте позже.",
            )
        finally:
            if temp_output_path is not None:
                Path(temp_output_path).unlink(missing_ok=True)
    return delivered_successfully


def log_generation_output_delivery(
    outputs_count: int,
    delivery_method: str,
    *,
    content_type: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
) -> None:
    """Логировать только безопасные метаданные доставки результатов генерации."""
    logger.info(
        {
            "action": "generation_output_delivery",
            "outputs_count": outputs_count,
            "delivery_method": delivery_method,
            "content_type": content_type,
            "file_size_bytes": file_size_bytes,
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


async def download_output_file_to_temp(output_url: str) -> tuple[str, Optional[str], Optional[int]]:
    """Скачать output-файл во временный файл для последующей отправки в Telegram."""
    temp_path: Optional[str] = None
    bytes_written = 0
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream("GET", output_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type")
                content_length_header = response.headers.get("content-length")
                if content_length_header is not None:
                    try:
                        content_length = int(content_length_header)
                    except ValueError:
                        content_length = None
                    else:
                        if content_length > MAX_TELEGRAM_DOCUMENT_SIZE_BYTES:
                            raise OutputDeliveryTooLargeError()

                suffix = get_output_suffix_and_type(content_type)
                temp_file = tempfile.NamedTemporaryFile(prefix="wavespeed-output-", suffix=suffix, delete=False)
                temp_path = temp_file.name
                try:
                    async for chunk in response.aiter_bytes():
                        bytes_written += len(chunk)
                        if bytes_written > MAX_TELEGRAM_DOCUMENT_SIZE_BYTES:
                            raise OutputDeliveryTooLargeError()
                        temp_file.write(chunk)
                finally:
                    temp_file.close()
        return temp_path, content_type, bytes_written
    except Exception:
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)
        raise


def log_background_task_exception(task: asyncio.Task) -> None:
    """Забрать исключение фоновой задачи, чтобы не было unhandled task exception."""
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("Background generation task was cancelled")
    except Exception as exc:
        logger.exception("Background generation task failed: %s", exc)


async def cleanup_generation_file(temp_input_path: Optional[str]) -> None:
    """Удалить временный входной файл после завершения сценария."""
    if temp_input_path:
        Path(temp_input_path).unlink(missing_ok=True)


async def reset_generation_state(state: FSMContext) -> None:
    """Сбросить FSM генерации и промежуточные данные."""
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
    temp_input_path: str,
) -> None:
    """Выполнить submit и дождаться terminal результата, затем удалить временный файл."""
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
        delivery_success = await send_generation_outputs(bot, chat_id, result.outputs)
        if delivery_success:
            success_text = "Готово ✅"
            if get_generation_model(model_key).generation_type == "lipsync":
                success_text = "Готово ✅ Ваш аватар озвучен."
            await safe_send_bot_message(bot, chat_id, success_text)
        await safe_send_bot_message(bot, chat_id, "🏠 Главное меню", reply_markup=get_main_menu_keyboard())
    except WavespeedTimeoutError as exc:
        logger.exception("Wavespeed timeout while polling generation result: %s", exc)
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
            get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
        )
    except WavespeedFailedError as exc:
        logger.exception("Wavespeed failed while polling generation result: %s", exc)
        result = getattr(exc, "result", result)
        safe_error_message = None
        if result is not None:
            safe_error_message = sanitize_external_error_message(result.error)
        if not safe_error_message:
            safe_error_message = sanitize_external_error_message(exc.user_message)
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
            get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
        )
    except WavespeedNetworkError as exc:
        logger.exception("Wavespeed network error while polling generation result: %s", exc)
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
            get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
        )
    except Exception as exc:
        logger.exception("Error while polling generation result: %s", exc)
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
            get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
        )
    finally:
        ACTIVE_GENERATIONS.pop(user_id, None)
        await cleanup_generation_file(temp_input_path)
        await reset_generation_state(state)
        await wavespeed.close()


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


@router.callback_query(F.data == BACK_TO_MAIN)
async def back_to_generation_main_menu(callback: CallbackQuery, state: FSMContext):
    """Закрыть inline-экран генераций и вернуть главное меню."""
    log_generation_callback(callback)
    await reset_generation_state(state)
    await callback.message.edit_text("🏠 Главное меню", reply_markup=None)
    await callback.message.answer("Выбери нужный раздел.", reply_markup=get_main_menu_keyboard())
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
    await state.update_data(current_setting_key=setting_key)
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
    """Перейти от настроек к загрузке изображения."""
    log_generation_callback(callback)
    state_data = await state.get_data()
    await state.set_state(GenerationStates.waiting_for_image)
    await prompt_for_generation_input(
        callback.message,
        edit=True,
        is_lipsync=is_lipsync_generation_state(state_data),
    )
    await callback.answer()


@router.message(GenerationStates.waiting_for_image, lambda message: message.text == "⬅️ Назад к настройкам")
async def back_to_settings_from_image_step(message: Message, state: FSMContext):
    """Вернуться с этапа загрузки изображения к настройкам модели без потери выбранных значений."""
    await state.set_state(GenerationStates.choosing_settings)
    await message.answer("Возвращаю к настройкам модели.", reply_markup=ReplyKeyboardRemove())
    await render_settings_screen_message(message, state, edit=False)


@router.message(GenerationStates.waiting_for_image, lambda message: bool(message.photo) or bool(message.document) or bool(message.video))
async def process_generation_image(message: Message, state: FSMContext):
    """Принять изображение для генерации."""
    state_data = await state.get_data()
    is_lipsync = is_lipsync_generation_state(state_data)
    document = message.document
    photo = message.photo[-1] if message.photo else None
    video = message.video

    if document and not is_supported_media_document(document, is_lipsync=is_lipsync):
        await message.answer(
            "❌ Нужны фото лица или видео. Отправь фото, video или document с форматом image/* или video/*."
            if is_lipsync
            else "❌ Нужен файл изображения. Отправь фото Telegram или document с форматом image/*.",
            reply_markup=get_main_menu_keyboard(),
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
        get_second_step_prompt_text(is_lipsync=is_lipsync),
        reply_markup=ReplyKeyboardRemove(),
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
        is_lipsync = is_lipsync_generation_state(state_data)
        input_image_file_id = state_data.get("input_image_file_id")
        input_media = state_data.get("input_media")
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
            prompt = (message.text or "").strip()
            if not prompt:
                await message.answer("❌ Отправь текстовый prompt.", reply_markup=get_main_menu_keyboard())
                return
            if len(prompt) < 10:
                await message.answer("❌ Описание слишком короткое (минимум 10 символов)")
                return
            if not input_image_file_id:
                await message.answer("❌ Сначала отправь изображение.", reply_markup=build_back_to_settings_keyboard())
                await state.set_state(GenerationStates.waiting_for_image)
                return
        
        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        
        if not user:
            await message.answer("❌ Пользователь не найден")
            return
        
        if not await user_repo.has_enough_balance(user.id, GENERATION_COST):
            await message.answer("❌ Недостаточно средств. Минимум 1 кредит для генерации")
            return

        await state.update_data(prompt=prompt, input_audio_or_text=input_audio_or_text)
        await send_confirmation_screen(
            message=message,
            state=state,
            session=session,
            telegram_user=message.from_user,
            edit=False,
        )
    except Exception as e:
        logger.exception("Error in process_prompt: %s", e)
        await message.answer("❌ Произошла ошибка при обработке запроса")


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
    input_image_file_id = state_data.get("input_image_file_id")
    is_lipsync = is_lipsync_generation_state(state_data)
    user_settings = validate_model_settings(model_key, state_data.get("user_settings")) if model_key else {}

    if not all([model_key, model_title, model_endpoint, prompt, input_image_file_id]):
        await callback.message.answer(
            get_lipsync_incomplete_error_text() if is_lipsync else "❌ Данные генерации неполные. Начни заново.",
            reply_markup=get_main_menu_keyboard(),
        )
        await reset_generation_state(state)
        await callback.answer()
        return

    debited_balance = False
    debited_user_id: Optional[int] = None
    generation_request_id = None
    temp_input_path: Optional[str] = None
    task_started = False

    try:
        user_repo = UserRepository(session)
        generation_repo = GenerationRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
        debited_user_id = user.id

        if not await user_repo.decrease_balance(user.id, GENERATION_COST):
            log_balance_event("insufficient_balance", user.id, GENERATION_COST)
            await callback.message.answer(
                "Недостаточно кредитов. Пополните баланс в магазине.",
                reply_markup=get_main_menu_keyboard(),
            )
            await reset_generation_state(state)
            await callback.answer()
            return
        debited_balance = True
        log_balance_event("balance_debited", user.id, GENERATION_COST)

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
        generation_request_id = generation_request.id

        telegram_files = TelegramFilesService(callback.bot)
        temp_media = await telegram_files.download_temp_file_and_get_public_url(input_image_file_id)
        image_url = temp_media.public_url
        temp_input_path = str(temp_media.local_path)
        payload = build_payload(model_key, [image_url], prompt, user_settings)

        await state.set_state(GenerationStates.generating)
        task = asyncio.create_task(
            poll_generation_result(
                bot=callback.bot,
                state=state,
                user_id=user_id,
                chat_id=callback.message.chat.id,
                generation_request_id=generation_request_id,
                model_key=model_key,
                cost=generation_request.cost,
                payload=payload,
                temp_input_path=temp_input_path,
            )
        )
        task.add_done_callback(log_background_task_exception)
        task_started = True
        ACTIVE_GENERATIONS[user_id] = {
            "task": task,
            "generation_request_id": generation_request_id,
        }

        await callback.message.edit_text(
            (
                "Генерация запущена. Обычно это занимает до 1–2 минут.\n\n"
                f"Модель: <b>{escape(model_title)}</b>"
            ),
            parse_mode="HTML",
        )
        await callback.message.answer("Генерация выполняется в фоне. Результат придёт сюда автоматически.", reply_markup=get_main_menu_keyboard())
        await callback.answer()
    except ImageUploadError as exc:
        logger.exception("Image upload failed before generation start: %s", exc)
        if generation_request_id is not None:
            await GenerationRepository(session).update_generation_status(
                generation_request_id,
                "failed",
                error_message=exc.user_message,
            )
        if debited_balance and debited_user_id is not None:
            try:
                user_repo = UserRepository(session)
                await user_repo.increase_balance(debited_user_id, GENERATION_COST)
            except Exception as refund_exc:
                logger.exception("Error while refunding balance after image upload failure: %s", refund_exc)
        await state.update_data(input_image_file_id=None)
        await state.set_state(GenerationStates.waiting_for_image)
        await callback.message.answer(
            "❌ Не удалось запустить генерацию. Проверьте параметры и попробуйте снова.",
            reply_markup=get_main_menu_keyboard(),
        )
        await callback.answer()
    except Exception as exc:
        logger.exception("Error while launching generation: %s", exc)
        if generation_request_id is not None:
            await GenerationRepository(session).update_generation_status(
                generation_request_id,
                "failed",
                error_message="Не удалось запустить генерацию. Попробуйте позже.",
            )
        if debited_balance and debited_user_id is not None:
            try:
                user_repo = UserRepository(session)
                await user_repo.increase_balance(debited_user_id, GENERATION_COST)
            except Exception as refund_exc:
                logger.exception("Error while refunding balance after launch failure: %s", refund_exc)
        await state.clear()
        await callback.message.answer(
            "❌ Не удалось запустить генерацию. Проверьте параметры и попробуйте снова.",
            reply_markup=get_main_menu_keyboard(),
        )
        await callback.answer()
    finally:
        if not task_started:
            await cleanup_generation_file(temp_input_path)


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
