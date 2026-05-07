"""Роутер магазина."""
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.keyboards import get_back_to_menu_keyboard, get_main_menu_keyboard
from app.bot.routers.generations import is_generation_flow_state, reset_generation_flow
from app.utils import logger


router = Router()


@router.message(lambda msg: msg.text == "🛒 Магазин")
async def show_shop(message: Message, state: FSMContext):
    """Показать магазин."""
    try:
        current_state = await state.get_state()
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason="main_menu_shop")
            await message.answer(
                "Сценарий генерации сброшен. Вы вернулись в главное меню.",
                reply_markup=get_main_menu_keyboard(),
            )

        shop_text = (
            "🛍 <b>Магазин</b>\n\n"
            "Магазин скоро будет доступен. Здесь можно будет пополнить баланс."
        )
        
        await message.answer(
            shop_text,
            reply_markup=get_back_to_menu_keyboard(),
            parse_mode="HTML",
        )
        
        logger.debug(f"Shop menu shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_shop: %s", e)
        await message.answer("❌ Произошла ошибка при открытии магазина")
