"""Роутер профиля пользователя."""
from datetime import datetime
from typing import Optional

from aiogram import Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import get_back_to_menu_keyboard, get_main_menu_keyboard
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


@router.message(lambda msg: msg.text == "👤 Профиль")
async def show_profile(message: Message, session: AsyncSession):
    """Показать профиль пользователя."""
    try:
        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        
        if not user:
            await message.answer("❌ Пользователь не найден")
            return
        
        profile_text = (
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
            f"📅 Дата регистрации: {format_datetime(user.created_at)}\n"
            f"🕒 Последняя активность: {format_datetime(user.last_seen_at)}"
        )
        
        await message.answer(
            profile_text,
            reply_markup=get_back_to_menu_keyboard(),
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
