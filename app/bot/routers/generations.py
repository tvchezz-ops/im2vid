"""Роутер генерации контента."""
import asyncio
from html import escape
from pathlib import Path
import tempfile
from typing import Any, Dict, Optional

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message, ReplyKeyboardRemove
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import (
    build_back_to_settings_keyboard,
    build_generation_confirm_keyboard,
    build_model_selection_keyboard,
    build_model_settings_keyboard,
    build_setting_options_keyboard,
    get_option_value_by_index,
    get_setting_key_by_index,
    get_main_menu_keyboard,
)
from app.bot.states import GenerationStates
from app.db import GenerationRepository, UserRepository
from app.db.session import db_manager
from app.services.generation_service import (
    GenerationModel,
    build_payload,
    get_default_settings,
    get_generation_model,
    list_generation_models,
    validate_model_settings,
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

MODEL_PREFIX = "gen:model:"
SETTINGS_OPEN_PREFIX = "gen:setting:"
SETTINGS_VALUE_PREFIX = "gen:set:"
SETTINGS_BACK_PREFIX = "gen:back_settings"
SETTINGS_BACK_MODELS = "gen:back_models"
SETTINGS_CONTINUE = "gen:continue"
SETTINGS_EDIT = "gen:edit"
GENERATION_CONFIRM = "gen:confirm"


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
    return (
        f"Настройки модели: <b>{escape(model.title)}</b>\n\n"
        f"Выберите параметры или нажмите Продолжить.\n\n"
        f"Текущие значения:\n{format_generation_settings(model, user_settings)}"
    )


def build_setting_value_text(model: GenerationModel, setting_key: str, current_value: str) -> str:
    """Собрать экран выбора конкретной настройки."""
    setting = model.user_settings[setting_key]
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
    return (
        f"Проверьте генерацию:\n\n"
        f"Модель: <b>{escape(model.title)}</b>\n"
        f"Настройки:\n{format_generation_settings(model, user_settings)}\n\n"
        f"Prompt: <i>{escape(prompt)}</i>\n\n"
        f"Стоимость: 1 кредит\n"
        f"Баланс после запуска: <code>{balance_after_launch}</code>"
    )


def get_user_friendly_error_message(error: Exception, result: Optional[WavespeedResult] = None) -> str:
    """Вернуть безопасное и понятное сообщение об ошибке для пользователя."""
    if isinstance(error, WavespeedTimeoutError):
        return "⏱ Генерация заняла слишком много времени и была остановлена. Кредит возвращён."

    if result is not None and result.status == "failed":
        normalized_error = (result.error or "").strip().lower()
        if "nsfw" in normalized_error:
            return "🚫 Генерация отклонена системой безопасности (NSFW контент). Попробуйте изменить запрос."
        return "❌ Генерация не удалась. Попробуйте изменить описание или настройки."

    if isinstance(error, TelegramBadRequest):
        return "⚠️ Генерация выполнена, но Telegram не смог доставить результат. Попробуйте позже."

    if isinstance(error, (WavespeedNetworkError, httpx.HTTPError, httpx.TimeoutException, TimeoutError)):
        return "🌐 Ошибка сети при получении результата. Попробуйте позже."

    return "⚠️ Произошла неизвестная ошибка. Попробуйте ещё раз."


def get_model_state_settings(state_data: dict[str, Any], model_key: str) -> dict[str, Any]:
    """Получить провалидированные настройки модели из FSM."""
    return validate_model_settings(model_key, state_data.get("user_settings"))


async def render_models_screen(message: Message) -> None:
    """Показать список моделей генерации."""
    generation_text = "Выберите модель для генерации/редактирования изображения:"
    await message.answer(
        generation_text,
        reply_markup=build_model_selection_keyboard(list_generation_models()),
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
    if edit:
        await message.edit_text(
            "Отправьте изображение как фото или файлом.",
            reply_markup=None,
        )
    else:
        await message.answer("Отправьте изображение как фото или файлом.")

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
    setting_entries = list(model.user_settings)
    setting_index = setting_entries.index(setting_key)
    await message.edit_text(
        build_setting_value_text(model, setting_key, current_value),
        reply_markup=build_setting_options_keyboard(
            setting_index,
            model.user_settings[setting_key].options,
            current_value,
        ),
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
    prompt = (state_data.get("prompt") or "").strip()
    input_image_file_id = state_data.get("input_image_file_id")

    if not model_key or not prompt or not input_image_file_id:
        if edit:
            await message.edit_text("❌ Данные генерации неполные. Начни заново.", reply_markup=None)
        else:
            await message.answer("❌ Данные генерации неполные. Начни заново.", reply_markup=get_main_menu_keyboard())
        await reset_generation_state(state)
        return

    model = get_generation_model(model_key)
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


async def send_generation_outputs(bot, chat_id: int, output_urls: list[str]) -> bool:
    """Отправить пользователю результаты генерации только как document через временный локальный файл."""
    delivered_successfully = True
    for index, output_url in enumerate(output_urls, start=1):
        caption = "Готово ✅" if index == 1 else None
        temp_output_path: Optional[str] = None
        try:
            temp_output_path, content_type, file_size_bytes = await download_output_file_to_temp(output_url)
            await bot.send_document(
                chat_id,
                FSInputFile(temp_output_path),
                caption=caption,
            )
            log_generation_output_delivery(
                len(output_urls),
                "downloaded_document",
                content_type=content_type,
                file_size_bytes=file_size_bytes,
            )
        except Exception as exc:
            delivered_successfully = False
            logger.exception(
                "Failed to deliver completed Wavespeed output as document: %s",
                type(exc).__name__,
            )
            log_generation_output_delivery(len(output_urls), "delivery_failed")
            await bot.send_message(
                chat_id,
                "⚠️ Генерация завершена, но файл не удалось отправить. Попробуйте позже.",
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
    response: Optional[httpx.Response] = None
    temp_path: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(output_url)
            response.raise_for_status()

        content_type = response.headers.get("content-type")
        suffix = get_output_suffix_and_type(content_type)
        temp_file = tempfile.NamedTemporaryFile(prefix="wavespeed-output-", suffix=suffix, delete=False)
        temp_path = temp_file.name
        temp_file.close()
        Path(temp_path).write_bytes(response.content)
        return temp_path, content_type, len(response.content)
    except Exception:
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)
        raise


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
            await bot.send_message(chat_id, "Готово ✅")
        await bot.send_message(chat_id, "🏠 Главное меню", reply_markup=get_main_menu_keyboard())
    except WavespeedTimeoutError as exc:
        logger.exception("Wavespeed timeout while polling generation result: %s", exc)
        await mark_generation_failed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            cost=cost,
            error_message="Wavespeed polling timed out",
            refund_credit=True,
            status="timeout",
        )
        await bot.send_message(
            chat_id,
            get_user_friendly_error_message(exc, result),
            reply_markup=get_main_menu_keyboard(),
        )
    except WavespeedFailedError as exc:
        logger.exception("Wavespeed failed while polling generation result: %s", exc)
        result = getattr(exc, "result", result)
        await mark_generation_failed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            cost=cost,
            error_message=exc.user_message,
            refund_credit=True,
        )
        await bot.send_message(
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
        await bot.send_message(
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
        await bot.send_message(
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
        await render_models_screen(message)
        
        logger.debug(f"Generation menu shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_generation_menu: %s", e)
        await message.answer("❌ Произошла ошибка при открытии меню генерации")


@router.callback_query(lambda cb: cb.data.startswith(MODEL_PREFIX))
async def choose_generation_model(callback: CallbackQuery, state: FSMContext):
    """Выбрать модель для генерации."""
    model_key = callback.data.removeprefix(MODEL_PREFIX)
    if callback.from_user.id in ACTIVE_GENERATIONS:
        await callback.answer("У вас уже запущена генерация", show_alert=True)
        return

    model = get_generation_model(model_key)
    await state.set_state(GenerationStates.choosing_settings)
    await state.update_data(
        model_key=model.key,
        model_title=model.title,
        model_endpoint=model.endpoint,
        user_settings=get_default_settings(model.key),
        current_setting_key=None,
        input_image_file_id=None,
        prompt=None,
    )
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.callback_query(lambda cb: cb.data == SETTINGS_BACK_MODELS)
async def back_to_generation_models(callback: CallbackQuery, state: FSMContext):
    """Вернуться к выбору модели."""
    await reset_generation_state(state)
    await callback.message.edit_text(
        "Выберите модель для генерации/редактирования изображения:",
        reply_markup=build_model_selection_keyboard(list_generation_models()),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(SETTINGS_OPEN_PREFIX))
async def open_setting_selector(callback: CallbackQuery, state: FSMContext):
    """Открыть выбор значения настройки модели."""
    setting_index_raw = callback.data.removeprefix(SETTINGS_OPEN_PREFIX)
    if not setting_index_raw.isdigit():
        await callback.answer("Настройка не найдена", show_alert=True)
        return
    setting_index = int(setting_index_raw)
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    if not model_key:
        await callback.message.edit_text("❌ Модель не выбрана. Начни заново.", reply_markup=None)
        await callback.answer()
        return
    model = get_generation_model(model_key)
    setting_key = get_setting_key_by_index(model, setting_index)
    if setting_key is None:
        await callback.answer("Настройка не найдена", show_alert=True)
        return
    await state.update_data(current_setting_key=setting_key)
    await state.set_state(GenerationStates.choosing_setting_value)
    await show_setting_options(callback.message, state, setting_key)
    await callback.answer()


@router.callback_query(lambda cb: cb.data == SETTINGS_BACK_PREFIX)
async def back_to_settings(callback: CallbackQuery, state: FSMContext):
    """Вернуться на экран настроек модели."""
    await state.set_state(GenerationStates.choosing_settings)
    await state.update_data(current_setting_key=None)
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith(SETTINGS_VALUE_PREFIX))
async def choose_setting_value(callback: CallbackQuery, state: FSMContext):
    """Сохранить выбранное значение настройки и вернуться к экрану настроек."""
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer("Некорректное значение", show_alert=True)
        return
    _, _, setting_index_raw, option_index_raw = parts
    if not setting_index_raw.isdigit() or not option_index_raw.isdigit():
        await callback.answer("Некорректное значение", show_alert=True)
        return
    state_data = await state.get_data()
    model_key = state_data.get("model_key")
    if not model_key:
        await callback.message.edit_text("❌ Модель не выбрана. Начни заново.", reply_markup=None)
        await callback.answer()
        return

    model = get_generation_model(model_key)
    setting_key = get_setting_key_by_index(model, int(setting_index_raw))
    if setting_key is None:
        await callback.answer("Настройка не найдена", show_alert=True)
        return

    user_settings = get_model_state_settings(state_data, model_key)
    selected_value = get_option_value_by_index(model, setting_key, int(option_index_raw))
    if selected_value is None:
        await callback.answer("Некорректное значение", show_alert=True)
        return
    user_settings[setting_key] = selected_value
    await state.update_data(user_settings=user_settings, current_setting_key=None)
    await state.set_state(GenerationStates.choosing_settings)
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.callback_query(lambda cb: cb.data == SETTINGS_CONTINUE)
async def continue_after_settings(callback: CallbackQuery, state: FSMContext):
    """Перейти от настроек к загрузке изображения."""
    await state.set_state(GenerationStates.waiting_for_image)
    await prompt_for_generation_image(callback.message, edit=True)
    await callback.answer()


@router.message(GenerationStates.waiting_for_image, lambda message: message.text == "⬅️ Назад к настройкам")
async def back_to_settings_from_image_step(message: Message, state: FSMContext):
    """Вернуться с этапа загрузки изображения к настройкам модели без потери выбранных значений."""
    await state.set_state(GenerationStates.choosing_settings)
    await message.answer("Возвращаю к настройкам модели.", reply_markup=ReplyKeyboardRemove())
    await render_settings_screen_message(message, state, edit=False)


@router.message(GenerationStates.waiting_for_image, lambda message: bool(message.photo) or bool(message.document))
async def process_generation_image(message: Message, state: FSMContext):
    """Принять изображение для генерации."""
    document = message.document
    photo = message.photo[-1] if message.photo else None

    if document and not ((document.mime_type or "").startswith("image/")):
        await message.answer(
            "❌ Нужен файл изображения. Отправь фото Telegram или document с форматом image/*.",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    file_id = document.file_id if document else photo.file_id
    await state.update_data(input_image_file_id=file_id)
    await state.set_state(GenerationStates.waiting_for_prompt)
    await message.answer(
        "Опишите, что нужно изменить на изображении.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(GenerationStates.waiting_for_image)
async def invalid_generation_image(message: Message):
    """Сообщить, что ожидается изображение."""
    await message.answer(
        "❌ Я жду изображение. Отправь фото Telegram или document с форматом image/*.",
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
        prompt = (message.text or "").strip()
        state_data = await state.get_data()
        model_key = state_data.get("model_key", "nano_banana")
        input_image_file_id = state_data.get("input_image_file_id")
        
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

        await state.update_data(prompt=prompt)
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


@router.callback_query(lambda cb: cb.data == SETTINGS_EDIT)
async def edit_generation_settings(callback: CallbackQuery, state: FSMContext):
    """Вернуться из подтверждения к настройкам модели."""
    await state.set_state(GenerationStates.choosing_settings)
    await render_settings_screen(callback.message, state)
    await callback.answer()


@router.callback_query(lambda cb: cb.data == GENERATION_CONFIRM)
async def confirm_generation(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    """Запустить генерацию после подтверждения."""
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
    user_settings = validate_model_settings(model_key, state_data.get("user_settings")) if model_key else {}

    if not all([model_key, model_title, model_endpoint, prompt, input_image_file_id]):
        await callback.message.answer("❌ Данные генерации неполные. Начни заново.", reply_markup=get_main_menu_keyboard())
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
