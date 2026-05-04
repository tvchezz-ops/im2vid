"""Глобальный обработчик ошибок aiogram."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import ErrorEvent

from app.bot.keyboards import get_main_menu_keyboard
from app.utils import get_friendly_error_message, logger


router = Router()


@router.error()
async def global_error_handler(event: ErrorEvent):
    """Логировать traceback и отдавать безопасное сообщение пользователю."""
    logger.exception("Unhandled aiogram exception: %s", event.exception)

    user_message = get_friendly_error_message(event.exception)
    update = event.update
    callback_query = getattr(update, "callback_query", None)
    message = getattr(update, "message", None)

    try:
        if callback_query is not None:
            try:
                await callback_query.answer("Произошла ошибка", show_alert=False)
            except Exception:
                pass
            target_message = callback_query.message
            if target_message is not None:
                await target_message.answer(user_message, reply_markup=get_main_menu_keyboard())
                return True

        if message is not None:
            await message.answer(user_message, reply_markup=get_main_menu_keyboard())
            return True
    except Exception:
        logger.exception("Failed to send friendly error message to user")

    return True