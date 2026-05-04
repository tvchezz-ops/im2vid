"""Роутер генерации контента."""
import asyncio
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import (
    get_back_to_menu_keyboard,
    get_cancel_keyboard,
    get_generation_confirm_keyboard,
    get_generation_models_keyboard,
    get_main_menu_keyboard,
)
from app.bot.states import GenerationStates
from app.db import GenerationRepository, UserRepository
from app.db.session import db_manager
from app.services.generation_service import get_generation_model
from app.services.telegram_files import TelegramFilesService
from app.services.wavespeed import WavespeedService
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
POLL_INTERVAL_SECONDS = 4
POLL_TIMEOUT_SECONDS = 120
GENERATION_COST = 1


def get_model_config(model_key: str) -> Dict[str, str]:
    """Получить конфиг модели по ключу."""
    try:
        model = get_generation_model(model_key)
    except ValueError:
        model = get_generation_model("nano_banana")
    return {
        "title": model.title,
        "endpoint": model.endpoint,
        "type": model.model_type,
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


async def mark_generation_failed(
    *,
    generation_request_id,
    user_id: int,
    model_key: str,
    cost: int,
    error_message: str,
    refund_credit: bool,
) -> None:
    """Обновить статус генерации как failed и при необходимости вернуть кредит."""
    async with db_manager.session_factory() as session:
        generation_repo = GenerationRepository(session)
        user_repo = UserRepository(session)
        await generation_repo.update_generation_status(
            generation_request_id,
            "failed",
            error_message=error_message,
        )
        if refund_credit:
            await user_repo.increase_balance(user_id, cost)
        await user_repo.increment_user_generation_stats(user_id, success=False)
    await log_generation_event(generation_request_id, user_id, model_key, "failed")


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
            output_urls=[],
            nsfw_flags=nsfw_flags,
        )
        await user_repo.increment_user_generation_stats(user_id, success=True)
    await log_generation_event(generation_request_id, user_id, model_key, "completed", output_count)


async def send_generation_outputs(bot, chat_id: int, output_urls: list[str]) -> None:
    """Отправить пользователю результаты генерации напрямую по URL."""
    for index, output_url in enumerate(output_urls, start=1):
        caption = "✅ Генерация завершена" if index == 1 else None
        try:
            await bot.send_photo(chat_id, output_url, caption=caption)
        except Exception:
            await bot.send_document(chat_id, output_url, caption=caption)


async def cleanup_generation_file(temp_input_path: str | None) -> None:
    """Удалить временный входной файл после завершения сценария."""
    if temp_input_path:
        Path(temp_input_path).unlink(missing_ok=True)


async def reset_generation_state(state: FSMContext) -> None:
    """Сбросить FSM генерации и промежуточные данные."""
    await state.clear()


async def cancel_active_generation(user_id: int) -> None:
    """Поставить флаг отмены для активной генерации."""
    active = ACTIVE_GENERATIONS.get(user_id)
    if active:
        active["cancel_event"].set()


async def poll_generation_result(
    *,
    bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    generation_request_id,
    model_key: str,
    prediction_id: str,
    cost: int,
    cancel_event: asyncio.Event,
    temp_input_path: str,
) -> None:
    """Опросить Wavespeed до завершения генерации."""
    wavespeed = WavespeedService()
    try:
        started = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started < POLL_TIMEOUT_SECONDS:
            if cancel_event.is_set():
                await mark_generation_failed(
                    generation_request_id=generation_request_id,
                    user_id=user_id,
                    model_key=model_key,
                    cost=cost,
                    error_message="Cancelled by user",
                    refund_credit=True,
                )
                return

            status_payload = await wavespeed.get_result(prediction_id)
            status = normalize_status(status_payload)

            if status == "completed":
                output_urls = extract_output_urls(status_payload)
                if not output_urls:
                    await mark_generation_failed(
                        generation_request_id=generation_request_id,
                        user_id=user_id,
                        model_key=model_key,
                        cost=cost,
                        error_message="Сервис генерации вернул пустой результат. Попробуйте позже.",
                        refund_credit=True,
                    )
                    await bot.send_message(chat_id, "❌ Сервис генерации вернул пустой результат. Попробуйте позже.")
                    await bot.send_message(chat_id, "🏠 Главное меню", reply_markup=get_main_menu_keyboard())
                    return

                await mark_generation_completed(
                    generation_request_id=generation_request_id,
                    user_id=user_id,
                    model_key=model_key,
                    nsfw_flags=status_payload.get("nsfw_flags"),
                    output_count=len(output_urls),
                )
                await send_generation_outputs(bot, chat_id, output_urls)
                await bot.send_message(chat_id, "🏠 Главное меню", reply_markup=get_main_menu_keyboard())
                return

            if status == "failed":
                error_message = sanitize_external_error_message(
                    status_payload.get("error") or status_payload.get("error_message") or status_payload.get("message")
                ) or "Генерация завершилась с ошибкой. Попробуйте позже."
                await mark_generation_failed(
                    generation_request_id=generation_request_id,
                    user_id=user_id,
                    model_key=model_key,
                    cost=cost,
                    error_message=error_message,
                    refund_credit=True,
                )
                await bot.send_message(chat_id, f"❌ Генерация завершилась с ошибкой:\n{error_message}")
                await bot.send_message(chat_id, "🏠 Главное меню", reply_markup=get_main_menu_keyboard())
                return

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        await mark_generation_failed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            cost=cost,
            error_message="Timeout while waiting for generation result",
            refund_credit=True,
        )
        await bot.send_message(
            chat_id,
            "❌ Генерация заняла слишком много времени. Попробуйте позже.",
            reply_markup=get_main_menu_keyboard(),
        )
    except WavespeedTimeoutError as exc:
        logger.exception("Wavespeed timeout while polling generation result: %s", exc)
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
            "❌ Генерация заняла слишком много времени. Попробуйте позже.",
            reply_markup=get_main_menu_keyboard(),
        )
    except WavespeedFailedError as exc:
        logger.exception("Wavespeed failed while polling generation result: %s", exc)
        await mark_generation_failed(
            generation_request_id=generation_request_id,
            user_id=user_id,
            model_key=model_key,
            cost=cost,
            error_message=exc.user_message,
            refund_credit=True,
        )
        await bot.send_message(chat_id, f"❌ {exc.user_message}", reply_markup=get_main_menu_keyboard())
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
            "❌ Не удалось получить статус генерации. Попробуйте позже.",
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
        await bot.send_message(chat_id, "❌ Ошибка при получении результата генерации.", reply_markup=get_main_menu_keyboard())
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
                "⚠️ У вас уже есть активный сценарий генерации. Завершите его или нажмите ❌ Отмена.",
                reply_markup=get_cancel_keyboard(),
            )
            return

        generation_text = (
            "🎨 <b>Генерации</b>\n\n"
            "Выбери модель для редактирования или генерации изображения.\n"
            "Nano Banana подходит для аккуратного редактирования, Seedream — для более креативных результатов."
        )
        
        await message.answer(
            generation_text,
            reply_markup=get_generation_models_keyboard(),
            parse_mode="HTML",
        )
        
        logger.debug(f"Generation menu shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_generation_menu: %s", e)
        await message.answer("❌ Произошла ошибка при открытии меню генерации")


@router.callback_query(lambda cb: cb.data.startswith("generation:model:"))
async def choose_generation_model(callback: CallbackQuery, state: FSMContext):
    """Выбрать модель для генерации."""
    model_key = callback.data.split(":")[-1]
    model_config = get_model_config(model_key)
    if callback.from_user.id in ACTIVE_GENERATIONS:
        await callback.answer("У вас уже запущена генерация", show_alert=True)
        return

    await state.set_state(GenerationStates.waiting_for_image)
    await state.update_data(
        model_key=model_key,
        model_title=model_config["title"],
        model_endpoint=model_config["endpoint"],
    )
    await callback.message.edit_text(
        (
            f"🖼 Выбрана модель: <b>{model_config['title']}</b>\n\n"
            "Теперь отправь изображение как photo или document."
        ),
        reply_markup=None,
        parse_mode="HTML",
    )
    await callback.message.answer(
        "На любом этапе можно отменить сценарий.",
        reply_markup=get_cancel_keyboard(),
    )
    await callback.answer()


@router.message(GenerationStates.waiting_for_image, lambda message: bool(message.photo) or bool(message.document))
async def process_generation_image(message: Message, state: FSMContext):
    """Принять изображение для генерации."""
    document = message.document
    photo = message.photo[-1] if message.photo else None

    if document and not ((document.mime_type or "").startswith("image/")):
        await message.answer(
            "❌ Нужен файл изображения. Отправь фото Telegram или document с форматом image/*.",
            reply_markup=get_cancel_keyboard(),
        )
        return

    file_id = document.file_id if document else photo.file_id
    await state.update_data(input_image_file_id=file_id)
    await state.set_state(GenerationStates.waiting_for_prompt)
    await message.answer(
        "✍️ Изображение получено. Теперь отправь текстовый prompt.",
        reply_markup=get_cancel_keyboard(),
    )


@router.message(GenerationStates.waiting_for_image)
async def invalid_generation_image(message: Message):
    """Сообщить, что ожидается изображение."""
    await message.answer(
        "❌ Я жду изображение. Отправь фото Telegram или document с форматом image/*.",
        reply_markup=get_cancel_keyboard(),
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
        model_title = state_data.get("model_title", get_model_config(model_key)["title"])
        input_image_file_id = state_data.get("input_image_file_id")
        
        if not prompt:
            await message.answer("❌ Отправь текстовый prompt.", reply_markup=get_cancel_keyboard())
            return
        if len(prompt) < 10:
            await message.answer("❌ Описание слишком короткое (минимум 10 символов)")
            return
        if not input_image_file_id:
            await message.answer("❌ Сначала отправь изображение.", reply_markup=get_cancel_keyboard())
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
        await state.set_state(GenerationStates.waiting_for_confirmation)

        await message.answer(
            (
                "📋 <b>Подтверждение генерации</b>\n\n"
                f"Модель: <b>{escape(model_title)}</b>\n"
                f"Prompt: <i>{escape(prompt)}</i>"
            ),
            reply_markup=get_generation_confirm_keyboard(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("Error in process_prompt: %s", e)
        await message.answer("❌ Произошла ошибка при обработке запроса")


@router.callback_query(lambda cb: cb.data == "generation:confirm")
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
        telegram_files = TelegramFilesService(callback.bot)
        temp_media = await telegram_files.download_temp_file_and_get_public_url(input_image_file_id)
        image_url = temp_media.public_url
        temp_input_path = str(temp_media.local_path)

        user_repo = UserRepository(session)
        generation_repo = GenerationRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
        debited_user_id = user.id

        if not await user_repo.decrease_balance(user.id, GENERATION_COST):
            await callback.message.answer(
                "❌ Недостаточно средств. Пополни баланс и попробуй снова.",
                reply_markup=get_main_menu_keyboard(),
            )
            await reset_generation_state(state)
            await callback.answer()
            return
        debited_balance = True

        generation_request = await generation_repo.create_generation_request(
            user_id=user.id,
            model_key=model_key,
            model_endpoint=model_endpoint,
            prompt=prompt,
            input_image_file_ids=[],
            input_image_urls=[],
            cost=GENERATION_COST,
        )
        generation_request_id = generation_request.id

        wavespeed = WavespeedService()
        try:
            prediction_id, _prediction_payload = await wavespeed.submit_generation(
                model_key=model_key,
                images=[image_url],
                prompt=prompt,
                options={
                    "aspect_ratio": state_data.get("aspect_ratio"),
                    "resolution": state_data.get("resolution"),
                    "size": state_data.get("size"),
                    "output_format": state_data.get("output_format"),
                },
            )
        finally:
            await wavespeed.close()

        await generation_repo.update_generation_status(
            generation_request.id,
            "processing",
            wavespeed_prediction_id=prediction_id,
        )

        await state.set_state(GenerationStates.generating)
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            poll_generation_result(
                bot=callback.bot,
                state=state,
                user_id=user_id,
                chat_id=callback.message.chat.id,
                generation_request_id=generation_request.id,
                model_key=model_key,
                prediction_id=prediction_id,
                cost=generation_request.cost,
                cancel_event=cancel_event,
                temp_input_path=temp_input_path,
            )
        )
        task_started = True
        ACTIVE_GENERATIONS[user_id] = {
            "task": task,
            "cancel_event": cancel_event,
            "generation_request_id": generation_request.id,
        }
        await log_generation_event(generation_request.id, user.id, model_key, "processing")

        await callback.message.edit_text(
            (
                "🚀 Генерация запущена\n\n"
                f"Модель: <b>{escape(model_title)}</b>\n"
                "Я буду проверять статус каждые несколько секунд."
            ),
            parse_mode="HTML",
        )
        await callback.message.answer("Для остановки используй ❌ Отмена.", reply_markup=get_cancel_keyboard())
        await callback.answer()
    except ImageUploadError as exc:
        logger.exception("Image upload failed before generation start: %s", exc)
        await state.update_data(input_image_file_id=None)
        await state.set_state(GenerationStates.waiting_for_image)
        await callback.message.answer(exc.user_message, reply_markup=get_cancel_keyboard())
        await callback.answer()
    except WavespeedFailedError as exc:
        logger.exception("Wavespeed failed while launching generation: %s", exc)
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
                logger.exception("Error while refunding balance after launch failure: %s", refund_exc)
        await state.clear()
        await callback.message.answer(f"❌ {exc.user_message}", reply_markup=get_main_menu_keyboard())
        await callback.answer()
    except WavespeedTimeoutError as exc:
        logger.exception("Wavespeed timeout while launching generation: %s", exc)
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
                logger.exception("Error while refunding balance after launch failure: %s", refund_exc)
        await state.clear()
        await callback.message.answer(
            "❌ Генерация заняла слишком много времени. Попробуйте позже.",
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
            "❌ Не удалось запустить генерацию. Попробуйте позже.",
            reply_markup=get_main_menu_keyboard(),
        )
        await callback.answer()
    finally:
        if not task_started:
            await cleanup_generation_file(temp_input_path)


@router.callback_query(lambda cb: cb.data == "generation:cancel")
async def cancel_generation_callback(callback: CallbackQuery, state: FSMContext):
    """Отменить генерацию через inline-кнопку."""
    is_active = callback.from_user.id in ACTIVE_GENERATIONS
    await cancel_active_generation(callback.from_user.id)
    await reset_generation_state(state)
    await callback.message.edit_text("❌ Генерация отменена.")
    if is_active:
        await callback.message.answer("⏹ Останавливаю активную генерацию.", reply_markup=get_main_menu_keyboard())
    else:
        await callback.message.answer("🏠 Главное меню", reply_markup=get_main_menu_keyboard())
    await callback.answer()


@router.message(lambda msg: msg.text == "❌ Отмена")
async def cancel_generation_message(message: Message, state: FSMContext):
    """Отменить активный сценарий генерации по кнопке."""
    current_state = await state.get_state()
    if current_state not in {
        GenerationStates.waiting_for_image.state,
        GenerationStates.waiting_for_prompt.state,
        GenerationStates.waiting_for_confirmation.state,
        GenerationStates.generating.state,
    }:
        await message.answer("Нет активного сценария для отмены.", reply_markup=get_main_menu_keyboard())
        return

    is_active = message.from_user.id in ACTIVE_GENERATIONS
    await cancel_active_generation(message.from_user.id)
    await reset_generation_state(state)
    if is_active:
        await message.answer("⏹ Останавливаю активную генерацию.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("❌ Сценарий генерации отменен.", reply_markup=get_main_menu_keyboard())
