"""Роутер команды /start."""
from typing import Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import get_button_text, get_main_menu_keyboard, is_localized_button_text
from app.bot.routers.generations import is_generation_flow_state, reset_generation_flow
from app.db import UserRepository
from app.i18n import get_user_language, t
from app.utils import logger


router = Router()


def build_welcome_text(first_name: Optional[str], lang: str = "en") -> str:
    """Текст приветствия в главном меню."""
    display_name = first_name or t("main.welcome_friend", lang)
    return t("main.welcome", lang, display_name=display_name)


@router.message(Command("start"))
async def start_command(message: Message, state: FSMContext, session: AsyncSession):
    """Обработчик команды /start."""
    try:
        current_state = await state.get_state()
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason="command_start")
            reset_lang = get_user_language(getattr(message.from_user, "language_code", None))
            await message.answer(
                t("generation.scenario_reset", reset_lang),
                reply_markup=get_main_menu_keyboard(reset_lang),
            )
        elif current_state is not None:
            await state.clear()

        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        lang = get_user_language(user.language_code)
        
        await message.answer(
            build_welcome_text(user.first_name, lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
        
        logger.info(f"User {message.from_user.id} started the bot")
    except Exception as e:
        logger.exception("Error in start command: %s", e)
        lang = get_user_language(getattr(message.from_user, "language_code", None))
        await message.answer(t("main.start_error", lang))


@router.message(Command("menu"))
@router.message(Command("cancel"))
@router.message(lambda msg: is_localized_button_text(msg.text, "common.back", getattr(msg.from_user, "language_code", None)))
async def menu_command(message: Message, state: FSMContext, session: AsyncSession):
    """Вернуть пользователя в главное меню."""
    try:
        current_state = await state.get_state()
        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        lang = get_user_language(user.language_code)
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason=f"command_{(message.text or '').lstrip('/').lower() or 'menu_button'}")
            await message.answer(
                t("generation.scenario_reset", lang),
                reply_markup=get_main_menu_keyboard(lang),
            )
        elif current_state is not None:
            await state.clear()

        await message.answer(
            t("main.choose_section", lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
    except Exception as e:
        logger.exception("Error in menu command: %s", e)
        lang = get_user_language(getattr(message.from_user, "language_code", None))
        await message.answer(t("main.menu_open_error", lang))
