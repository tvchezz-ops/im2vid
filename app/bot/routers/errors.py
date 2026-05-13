"""Глобальный обработчик ошибок aiogram."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import ErrorEvent

from app.bot.error_messages import build_error_keyboard, build_user_error_message, log_error_code
from app.bot.keyboards import get_main_menu_keyboard
from app.i18n import get_user_language, t
from app.utils import logger


router = Router()


@router.error()
async def global_error_handler(event: ErrorEvent):
    """Логировать traceback и отдавать безопасное сообщение пользователю."""
    logger.exception("Unhandled aiogram exception: %s", event.exception)
    log_error_code("E010", {"action": "unhandled_aiogram_exception", "error": event.exception.__class__.__name__})

    update = event.update
    callback_query = getattr(update, "callback_query", None)
    message = getattr(update, "message", None)

    try:
        if callback_query is not None:
            lang = get_user_language(getattr(callback_query.from_user, "language_code", None))
            user_message = build_user_error_message("errors.internal", lang)
            try:
                await callback_query.answer(user_message, show_alert=False)
            except Exception:
                pass
            target_message = callback_query.message
            if target_message is not None:
                await target_message.answer(user_message, reply_markup=build_error_keyboard("errors.internal", lang) or get_main_menu_keyboard(lang))
                return True

        if message is not None:
            lang = get_user_language(getattr(message.from_user, "language_code", None))
            user_message = build_user_error_message("errors.internal", lang)
            await message.answer(user_message, reply_markup=build_error_keyboard("errors.internal", lang) or get_main_menu_keyboard(lang))
            return True
    except Exception:
        logger.exception("Failed to send friendly error message to user")

    return True