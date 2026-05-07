"""Роутер профиля пользователя."""
from datetime import datetime
from typing import Optional

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import get_main_menu_keyboard, get_profile_keyboard
from app.bot.routers.generations import is_generation_flow_state, reset_generation_flow
from app.db import UserRepository
from app.utils import logger


router = Router()


def format_datetime(value: Optional[datetime]) -> str:
    """Форматировать дату для Telegram UI."""
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_premium_status(value: Optional[bool]) -> str:
    """Показать premium-статус пользователя."""
    if value is None:
        return "неизвестно"
    return "да" if value else "нет"


def format_delivery_mode(send_results_as_files: bool) -> str:
    return "Файлом без сжатия" if send_results_as_files else "Обычный формат"


def build_profile_text(user) -> str:
    return (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"👤 Username: @{user.username if user.username else '—'}\n"
        f"📝 Имя: {(user.first_name or '—')} {(user.last_name or '')}\n"
        f"🌐 Язык: {user.language_code or '—'}\n"
        f"⭐ Premium: {format_premium_status(user.is_premium)}\n"
        f"💰 Баланс: {user.balance}\n"
        f"🎨 Всего генераций: {user.total_generations}\n"
        f"✅ Успешные: {user.successful_generations}\n"
        f"❌ Неуспешные: {user.failed_generations}\n"
        f"📦 Способ отправки: {format_delivery_mode(user.send_results_as_files)}\n\n"
        "Обычный формат быстрее и удобнее для просмотра.\n"
        "Файлом — без сжатия, лучше для качества.\n\n"
        f"📅 Дата регистрации: {format_datetime(user.created_at)}\n"
        f"🕒 Последняя активность: {format_datetime(user.last_seen_at)}"
    )


@router.message(lambda msg: msg.text == "👤 Профиль")
async def show_profile(message: Message, state: FSMContext, session: AsyncSession):
    """Показать профиль пользователя."""
    try:
        current_state = await state.get_state()
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason="main_menu_profile")
            await message.answer(
                "Сценарий генерации сброшен. Вы вернулись в главное меню.",
                reply_markup=get_main_menu_keyboard(),
            )

        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        
        if not user:
            await message.answer("❌ Пользователь не найден")
            return
        
        profile_text = build_profile_text(user)
        
        await message.answer(
            profile_text,
            reply_markup=get_profile_keyboard(send_results_as_files=user.send_results_as_files),
            parse_mode="HTML",
        )
        
        logger.debug(f"Profile shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_profile: %s", e)
        await message.answer("❌ Произошла ошибка при загрузке профиля")


@router.callback_query(lambda cb: cb.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    """Вернуться в главное меню."""
    await callback.message.edit_text(
        "🏠 Главное меню",
        reply_markup=None,
    )
    await callback.message.answer(
        "Выбери нужный раздел.",
        reply_markup=get_main_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "profile:toggle_delivery_mode")
async def toggle_delivery_mode(callback: CallbackQuery, session: AsyncSession):
    """Переключить способ отправки результатов и обновить экран профиля."""
    user_repo = UserRepository(session)
    await user_repo.get_or_create_user_from_telegram(callback.from_user)
    new_value = await user_repo.toggle_user_delivery_preference(callback.from_user.id)
    user = await user_repo.get_user_profile(callback.from_user.id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    user.send_results_as_files = new_value
    await callback.message.edit_text(
        build_profile_text(user),
        reply_markup=get_profile_keyboard(send_results_as_files=new_value),
        parse_mode="HTML",
    )
    await callback.answer("Настройка обновлена")
