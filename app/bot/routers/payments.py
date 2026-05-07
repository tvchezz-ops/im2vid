"""Payment top-up router."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from uuid import UUID

from app.bot.keyboards import (
    build_crypto_payment_keyboard,
    build_crypto_top_up_keyboard,
    build_stars_top_up_keyboard,
    build_wallet_bot_payment_keyboard,
    get_profile_keyboard,
)
from app.bot.routers.profile import build_profile_text
from app.config import is_nowpayments_configured, settings
from app.db import PaymentProvider, PaymentRepository, UserRepository
from app.i18n import get_user_language, t
from app.services.payments import ALLOWED_STARS_AMOUNTS
from app.services.nowpayments import NowPaymentsService
from app.utils import logger


router = Router()


@router.callback_query(lambda cb: cb.data == "profile:top_up_balance")
async def show_stars_top_up_menu(callback: CallbackQuery) -> None:
    """Показать меню выбора количества Telegram Stars."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    await callback.message.edit_text(
        t("payments.choose_stars_amount", lang),
        reply_markup=build_stars_top_up_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "pay:back:profile")
async def back_to_profile(callback: CallbackQuery, session: AsyncSession) -> None:
    """Вернуться из оплаты в профиль."""
    user_repo = UserRepository(session)
    user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
    lang = get_user_language(user.language_code)
    total_spent_credits = await user_repo.get_total_spent_credits(user.id)
    await callback.message.edit_text(
        build_profile_text(user, total_spent_credits, lang),
        reply_markup=get_profile_keyboard(send_results_as_files=user.send_results_as_files, lang=lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "pay:crypto")
async def show_crypto_packages(callback: CallbackQuery) -> None:
    """Показать crypto-пакеты кредитов."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    await callback.message.edit_text(
        t("payments.choose_method", lang),
        reply_markup=build_crypto_top_up_keyboard(lang),
    )
    await callback.answer()


def _parse_crypto_amount(callback_data: str | None) -> int | None:
    if callback_data is None or not callback_data.startswith("pay:crypto:"):
        return None
    amount_text = callback_data.removeprefix("pay:crypto:")
    if not amount_text.isdigit():
        return None
    return int(amount_text)


def calculate_crypto_amount_usd(credits: int) -> str:
    return f"{credits * 0.01:.2f}"


def _parse_stars_amount(callback_data: str | None) -> int | None:
    if callback_data is None or not callback_data.startswith("pay:stars:"):
        return None
    amount_text = callback_data.removeprefix("pay:stars:")
    if not amount_text.isdigit():
        return None
    return int(amount_text)


def _normalize_bot_username(username: str) -> str:
    return username.strip().lstrip("@")


def build_wallet_payment_url(wallet_bot_username: str, amount: int) -> str:
    """Build a Telegram deep link to an external wallet bot."""
    return f"https://t.me/{_normalize_bot_username(wallet_bot_username)}?start={amount}credits"


def build_wallet_return_url(payload: str) -> str | None:
    """Build the optional return deep link from an external wallet bot."""
    bot_username = _normalize_bot_username(settings.telegram_stars_return_bot_username)
    if not bot_username:
        return None
    return f"https://t.me/{bot_username}?start=paid_{payload}"


@router.callback_query(lambda cb: cb.data.startswith("pay:stars:"))
async def choose_stars_amount(callback: CallbackQuery) -> None:
    """Show a wallet bot deep link for the selected Stars package."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    amount = _parse_stars_amount(callback.data)
    if amount not in ALLOWED_STARS_AMOUNTS:
        await callback.answer(t("payments.invalid_amount", lang), show_alert=True)
        return

    wallet_bot_username = _normalize_bot_username(settings.wallet_bot_username or settings.telegram_stars_wallet_bot_username)
    if not wallet_bot_username:
        logger.warning({"action": "wallet_bot_not_configured", "user_id": callback.from_user.id, "amount": amount})
        await callback.answer(t("payments.invoice_error", lang), show_alert=True)
        return

    await callback.message.edit_text(
        "To pay with Telegram Stars, open the wallet bot.\nAfter payment, return here and your credits will already be added.",
        reply_markup=build_wallet_bot_payment_keyboard(
            amount=amount,
            wallet_payment_url=build_wallet_payment_url(wallet_bot_username, amount),
        )
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data.startswith("pay:crypto:") and not cb.data.startswith("pay:crypto:check:"))
async def choose_crypto_amount(callback: CallbackQuery, session: AsyncSession) -> None:
    """Создать NOWPayments crypto payment и показать ссылку на оплату."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    amount = _parse_crypto_amount(callback.data)
    if amount not in ALLOWED_STARS_AMOUNTS:
        await callback.answer(t("payments.invalid_amount", lang), show_alert=True)
        return
    if not is_nowpayments_configured():
        await callback.answer(t("payments.crypto_coming_soon", lang), show_alert=True)
        return

    try:
        payment_repo = PaymentRepository(session)
        order = await payment_repo.create_payment_order(
            user_id=callback.from_user.id,
            provider=PaymentProvider.CRYPTO.value,
            amount=amount,
            credits=amount,
            currency="USD",
            metadata={"provider": "nowpayments"},
        )
        await payment_repo.create_crypto_payment_order(
            order.id,
            price_amount=calculate_crypto_amount_usd(amount),
            price_currency="usd",
            status="created",
        )
        payment = await NowPaymentsService().create_payment(
            order_id=str(order.id),
            credits=amount,
            amount_usd=calculate_crypto_amount_usd(amount),
        )
        payment_id = str(payment.get("payment_id") or "")
        payment_url = payment.get("payment_url") or payment.get("invoice_url")
        if not payment_id or not payment_url:
            raise ValueError("NOWPayments response missing payment id or payment URL")

        await payment_repo.attach_nowpayments_payment_details(
            order.id,
            nowpayments_payment_id=payment_id,
            payment_url=str(payment_url),
            pay_address=payment.get("pay_address"),
            pay_currency=payment.get("pay_currency"),
            price_amount=str(payment.get("price_amount") or calculate_crypto_amount_usd(amount)),
            price_currency=str(payment.get("price_currency") or "usd"),
            status="pending",
        )
        await callback.message.edit_text(
            t("payments.external_ready", lang),
            reply_markup=build_crypto_payment_keyboard(payment_url=str(payment_url), order_id=str(order.id), lang=lang),
        )
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
        await callback.answer(t("payments.invoice_error", lang), show_alert=True)


@router.callback_query(lambda cb: cb.data.startswith("pay:crypto:check:"))
async def check_crypto_payment(callback: CallbackQuery, session: AsyncSession) -> None:
    """MVP check button: read local order status after webhook processing."""
    order_id = callback.data.removeprefix("pay:crypto:check:") if callback.data else ""
    try:
        parsed_order_id = UUID(order_id)
    except ValueError:
        await callback.answer(t("payments.pending", get_user_language(getattr(callback.from_user, "language_code", None))), show_alert=True)
        return
    order = await PaymentRepository(session).get_payment_order_by_id(parsed_order_id)
    if order is not None and order.user_id == callback.from_user.id and order.status == "paid":
        lang = get_user_language(getattr(callback.from_user, "language_code", None))
        user = await UserRepository(session).get_by_id(order.user_id)
        balance = user.balance if user is not None else order.credits
        await callback.answer(t("payments.received", lang, credits=order.credits, balance=balance), show_alert=True)
        return
    await callback.answer(t("payments.pending", get_user_language(getattr(callback.from_user, "language_code", None))), show_alert=True)
