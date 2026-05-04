"""Роутер команды /start."""
from typing import Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import get_main_menu_keyboard
from app.db import UserRepository
from app.utils import logger


router = Router()


def build_welcome_text(first_name: Optional[str]) -> str:
    """Текст приветствия в главном меню."""
    display_name = first_name or "друг"
    return (
        f"👋 Привет, {display_name}!\n\n"
        "Я умею помогать с генерацией и редактированием изображений через модели Nano Banana и Seedream.\n"
        "Выбирай раздел в меню: генерации, профиль или магазин."
    )


@router.message(Command("start"))
async def start_command(message: Message, session: AsyncSession):
    """Обработчик команды /start."""
    try:
        user_repo = UserRepository(session)
        
        # Создаем или обновляем пользователя из Telegram объекта
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        
        await message.answer(
            build_welcome_text(user.first_name),
            reply_markup=get_main_menu_keyboard(),
        )
        
        logger.info(f"User {message.from_user.id} started the bot")
    except Exception as e:
        logger.exception("Error in start command: %s", e)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")


@router.message(Command("menu"))
@router.message(lambda msg: msg.text == "⬅️ Назад в меню")
async def menu_command(message: Message, session: AsyncSession):
    """Вернуть пользователя в главное меню."""
    try:
        user_repo = UserRepository(session)
        await user_repo.update_user_seen(message.from_user.id)
        await message.answer(
            "🏠 Главное меню",
            reply_markup=get_main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("Error in menu command: %s", e)
        await message.answer("❌ Не удалось открыть главное меню.")
