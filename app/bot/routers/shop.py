"""Роутер магазина."""
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import get_back_to_menu_keyboard, get_main_menu_keyboard, is_localized_button_text
from app.bot.routers.generations import is_generation_flow_state, reset_generation_flow
from app.db import UserRepository
from app.i18n import get_user_language, t
from app.utils import logger


router = Router()


@router.message(lambda msg: is_localized_button_text(msg.text, "main.shop", getattr(msg.from_user, "language_code", None)))
async def show_shop(message: Message, state: FSMContext, session: AsyncSession):
    """Показать магазин."""
    try:
        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        lang = get_user_language(user.language_code if user is not None else getattr(message.from_user, "language_code", None))
        current_state = await state.get_state()
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason="main_menu_shop")
            await message.answer(
                t("generation.scenario_reset", lang),
                reply_markup=get_main_menu_keyboard(lang),
            )

        shop_text = (
            f"🛍 <b>{t('shop.title', lang)}</b>\n\n"
            f"{t('shop.stub', lang)}"
        )
        
        await message.answer(
            shop_text,
            reply_markup=get_back_to_menu_keyboard(lang),
            parse_mode="HTML",
        )
        
        logger.debug(f"Shop menu shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_shop: %s", e)
        lang = get_user_language(getattr(message.from_user, "language_code", None))
        await message.answer(f"❌ {t('shop.open_error', lang)}")
