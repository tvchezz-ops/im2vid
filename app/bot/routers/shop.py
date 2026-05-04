"""Роутер магазина."""
from aiogram import Router
from aiogram.types import Message

from app.bot.keyboards import get_back_to_menu_keyboard
from app.utils import logger


router = Router()


@router.message(lambda msg: msg.text == "🛒 Магазин")
async def show_shop(message: Message):
    """Показать магазин."""
    try:
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
