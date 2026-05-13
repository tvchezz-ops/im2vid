"""Payment top-up router."""
from __future__ import annotations

import re
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from urllib.parse import quote

from app.bot.error_messages import build_user_error_message
from app.bot.keyboards import (
    build_crypto_payment_keyboard,
    build_crypto_top_up_keyboard,
    build_stars_top_up_keyboard,
    build_stars_wallet_redirect_keyboard,
    build_top_up_method_keyboard,
    get_profile_keyboard,
)
from app.bot.language import get_event_lang
from app.bot.routers.profile import build_profile_text
from app.config import is_nowpayments_configured, settings
from app.db import UserRepository
from app.i18n import t
from app.services.nowpayments import NowPaymentsService
from app.services.payments import ALLOWED_STARS_AMOUNTS, PaymentService
from app.utils import logger


router = Router()
TELEGRAM_DEEP_LINK_PAYLOAD_LIMIT = 64
TELEGRAM_DEEP_LINK_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def safe_edit_message(message, text, reply_markup=None, **kwargs):
    try:
        await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        raise


async def _set_payment_screen(state: FSMContext | None, screen: str) -> None:
    if state is not None:
        await state.update_data(payment_screen=screen)


@router.callback_query(lambda cb: cb.data in {"profile:top_up_balance", "profile:topup"})
async def show_stars_top_up_menu(callback: CallbackQuery, state: FSMContext | None = None, session: AsyncSession | None = None) -> None:
    """Показать меню выбора способа оплаты."""
    lang = await get_event_lang(callback, session)
    logger.info({"action": "payment_menu_opened", "user_id": callback.from_user.id})
    await safe_edit_message(
        callback.message,
        t("payments.choose_method", lang),
        reply_markup=build_top_up_method_keyboard(lang),
    )
    await _set_payment_screen(state, "methods")
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "pay:method:stars")
async def show_stars_amount_menu(callback: CallbackQuery, state: FSMContext | None = None, session: AsyncSession | None = None) -> None:
    """Показать меню выбора количества Telegram Stars."""
    lang = await get_event_lang(callback, session)
    await safe_edit_message(
        callback.message,
        t("payments.choose_stars_amount", lang),
        reply_markup=build_stars_top_up_keyboard(lang),
    )
    await _set_payment_screen(state, "stars_amounts")
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "pay:back:methods")
async def back_to_payment_methods(callback: CallbackQuery, state: FSMContext | None = None, session: AsyncSession | None = None) -> None:
    """Вернуться к выбору способа оплаты."""
    lang = await get_event_lang(callback, session)
    await safe_edit_message(
        callback.message,
        t("payments.choose_method", lang),
        reply_markup=build_top_up_method_keyboard(lang),
    )
    await _set_payment_screen(state, "methods")
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "pay:back:stars_amounts")
async def back_to_stars_amounts(callback: CallbackQuery, state: FSMContext | None = None, session: AsyncSession | None = None) -> None:
    """Вернуться к выбору суммы Telegram Stars."""
    lang = await get_event_lang(callback, session)
    await safe_edit_message(
        callback.message,
        t("payments.choose_stars_amount", lang),
        reply_markup=build_stars_top_up_keyboard(lang),
    )
    await _set_payment_screen(state, "stars_amounts")
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "pay:back:crypto_amounts")
async def back_to_crypto_amounts(callback: CallbackQuery, state: FSMContext | None = None, session: AsyncSession | None = None) -> None:
    """Вернуться к выбору crypto-пакета."""
    lang = await get_event_lang(callback, session)
    await safe_edit_message(
        callback.message,
        t("payments.choose_method", lang),
        reply_markup=build_crypto_top_up_keyboard(lang),
    )
    await _set_payment_screen(state, "crypto_amounts")
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "pay:back:profile")
async def back_to_profile(callback: CallbackQuery, session: AsyncSession, state: FSMContext | None = None) -> None:
    """Вернуться из оплаты в профиль."""
    user_repo = UserRepository(session)
    lang = await get_event_lang(callback, session)
    user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
    total_spent_credits = await user_repo.get_total_spent_credits(user.id)
    await safe_edit_message(
        callback.message,
        build_profile_text(user, total_spent_credits, lang),
        reply_markup=get_profile_keyboard(send_results_as_files=user.send_results_as_files, lang=lang),
        parse_mode="HTML",
    )
    await _set_payment_screen(state, "profile")
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "pay:crypto")
async def show_crypto_packages(callback: CallbackQuery, state: FSMContext | None = None, session: AsyncSession | None = None) -> None:
    """Показать crypto-пакеты кредитов."""
    lang = await get_event_lang(callback, session)
    if not is_nowpayments_configured():
        logger.info({"action": "crypto_not_configured", "user_id": callback.from_user.id})
        await callback.answer(build_user_error_message("payments.crypto_not_configured", lang), show_alert=True)
        return
    await safe_edit_message(
        callback.message,
        t("payments.choose_method", lang),
        reply_markup=build_crypto_top_up_keyboard(lang),
    )
    await _set_payment_screen(state, "crypto_amounts")
    await callback.answer()


def _parse_crypto_amount(callback_data: str | None) -> int | None:
    if callback_data is None or not callback_data.startswith("pay:crypto:"):
        return None
    amount_text = callback_data.removeprefix("pay:crypto:")
    if not amount_text.isdigit():
        return None
    return int(amount_text)


def _parse_stars_amount(callback_data: str | None) -> int | None:
    if callback_data is None or not callback_data.startswith("pay:stars:"):
        return None
    amount_text = callback_data.removeprefix("pay:stars:")
    if not amount_text.isdigit():
        return None
    return int(amount_text)


def _normalize_bot_username(username: str | None) -> str:
    return (username or "").strip().lstrip("@")


def build_wallet_payment_url(wallet_bot_username: str, amount: int) -> str:
    """Build a Telegram deep link to an external wallet bot."""
    return f"https://t.me/{_normalize_bot_username(wallet_bot_username)}?start={amount}credits"


def build_wallet_payment_url_for_payload(wallet_bot_username: str, payload: str) -> str:
    """Build a Telegram deep link to an external wallet bot with the payment payload."""
    if len(payload) > TELEGRAM_DEEP_LINK_PAYLOAD_LIMIT or TELEGRAM_DEEP_LINK_PAYLOAD_RE.fullmatch(payload) is None:
        raise ValueError("Telegram deep-link payload must be URL-safe and no longer than 64 characters")
    return f"https://t.me/{_normalize_bot_username(wallet_bot_username)}?start={payload}"


def build_wallet_return_url(payload: str) -> str | None:
    """Build the optional return deep link from an external wallet bot."""
    bot_username = _normalize_bot_username(settings.telegram_stars_return_bot_username)
    if not bot_username:
        return None
    return f"https://t.me/{bot_username}?start=paid_{quote(payload, safe='')}"


@router.callback_query(lambda cb: cb.data.startswith("pay:stars:"))
async def choose_stars_amount(callback: CallbackQuery, session: AsyncSession, state: FSMContext | None = None) -> None:
    """Create a Telegram Stars order and redirect to the external wallet bot."""
    lang = await get_event_lang(callback, session)
    amount = _parse_stars_amount(callback.data)
    logger.info({"action": "stars_amount_selected", "user_id": callback.from_user.id, "amount": amount})
    if amount not in ALLOWED_STARS_AMOUNTS:
        await callback.answer(build_user_error_message("payments.invalid_amount", lang), show_alert=True)
        return

    try:
        wallet_bot_username = _normalize_bot_username(settings.wallet_bot_username)
        if not wallet_bot_username:
            logger.info(
                {
                    "action": "stars_wallet_not_configured",
                    "user_id": callback.from_user.id,
                    "amount": amount,
                }
            )
            await callback.answer(build_user_error_message("payments.stars_wallet_not_configured", lang), show_alert=True)
            return
        order = await PaymentService(session).create_stars_order(callback.from_user.id, amount)
        wallet_payment_url = build_wallet_payment_url_for_payload(wallet_bot_username, order.payload)
        logger.info(
            {
                "action": "stars_wallet_redirect_created",
                "wallet_bot": wallet_bot_username,
                "payload_length": len(order.payload or ""),
                "order_id": str(order.id),
            }
        )
        await safe_edit_message(
            callback.message,
            t("payments.stars_redirect_ready", lang, amount=amount),
            reply_markup=build_stars_wallet_redirect_keyboard(wallet_payment_url=wallet_payment_url, lang=lang),
        )
        await _set_payment_screen(state, "stars_redirect")
        await callback.answer()
    except Exception as exc:
        logger.exception(
            {
                "action": "telegram_stars_invoice_error",
                "user_id": callback.from_user.id,
                "amount": amount,
                "error": exc.__class__.__name__,
            }
        )
        await callback.answer(build_user_error_message("payments.invoice_error", lang), show_alert=True)


@router.callback_query(lambda cb: cb.data.startswith("pay:crypto:"))
async def choose_crypto_amount(callback: CallbackQuery, session: AsyncSession, state: FSMContext | None = None) -> None:
    """Создать NOWPayments checkout link и показать пользователю кнопку оплаты."""
    lang = await get_event_lang(callback, session)
    amount = _parse_crypto_amount(callback.data)
    if amount not in ALLOWED_STARS_AMOUNTS:
        await callback.answer(build_user_error_message("payments.invalid_amount", lang), show_alert=True)
        return
    if not is_nowpayments_configured():
        logger.info({"action": "crypto_not_configured", "user_id": callback.from_user.id})
        await callback.answer(build_user_error_message("payments.crypto_not_configured", lang), show_alert=True)
        return

    try:
        payment_link = await NowPaymentsService(session=session).create_payment_order_link(callback.from_user.id, amount)
        logger.info({"action": "crypto_invoice_created", "user_id": callback.from_user.id, "amount": amount})
        await safe_edit_message(
            callback.message,
            t(
                "payments.crypto_payment_details",
                lang,
                credits=amount,
                price_amount=payment_link.price_amount,
            ),
            reply_markup=build_crypto_payment_keyboard(payment_url=payment_link.payment_url, lang=lang),
        )
        await _set_payment_screen(state, "crypto_invoice")
        await callback.answer()
    except Exception as exc:
        logger.exception(
            {
                "action": "nowpayments_create_payment_error",
                "user_id": callback.from_user.id,
                "amount": amount,
                "error": exc.__class__.__name__,
            }
        )
        await callback.answer(build_user_error_message("payments.invoice_error", lang), show_alert=True)
