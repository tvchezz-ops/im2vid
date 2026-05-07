"""Payment top-up router."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from urllib.parse import quote
from uuid import UUID

from app.bot.keyboards import (
    build_stars_payment_method_keyboard,
    build_stars_top_up_keyboard,
    get_profile_keyboard,
)
from app.bot.routers.profile import build_profile_text
from app.config import settings
from app.db import PaymentProvider, PaymentRepository, UserRepository
from app.i18n import get_user_language, t
from app.services.payments import ALLOWED_STARS_AMOUNTS, PaymentOrderNotFoundError, PaymentService
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
async def show_crypto_payments_soon(callback: CallbackQuery) -> None:
    """Показать заглушку crypto payments."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    await callback.answer(t("payments.crypto_coming_soon", lang), show_alert=True)


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


def build_wallet_payment_url_for_payload(wallet_bot_username: str, payload: str) -> str:
    """Build a Telegram deep link to an external wallet bot with the payment payload."""
    return f"https://t.me/{_normalize_bot_username(wallet_bot_username)}?start={quote(payload, safe='')}"


def build_wallet_return_url(payload: str) -> str | None:
    """Build the optional return deep link from an external wallet bot."""
    bot_username = _normalize_bot_username(settings.telegram_stars_return_bot_username)
    if not bot_username:
        return None
    return f"https://t.me/{bot_username}?start=paid_{quote(payload, safe='')}"


async def send_stars_invoice(message: Message, order, lang: str) -> None:
    """Send a Telegram Stars Bot API invoice for an existing order."""
    await message.answer_invoice(
        title=t("payments.invoice_title", lang),
        description=t("payments.invoice_description", lang, amount=order.amount),
        payload=order.payload,
        currency="XTR",
        provider_token="",
        prices=[LabeledPrice(label=t("payments.invoice_label", lang, amount=order.amount), amount=order.amount)],
    )


@router.callback_query(lambda cb: cb.data.startswith("pay:stars:"))
async def choose_stars_amount(callback: CallbackQuery, session: AsyncSession) -> None:
    """Create a Telegram Stars order and send a Bot API invoice."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    amount = _parse_stars_amount(callback.data)
    if amount not in ALLOWED_STARS_AMOUNTS:
        await callback.answer(t("payments.invalid_amount", lang), show_alert=True)
        return

    try:
        order = await PaymentService(session).create_stars_order(callback.from_user.id, amount)
        wallet_bot_username = _normalize_bot_username(settings.telegram_stars_wallet_bot_username)
        if wallet_bot_username:
            await callback.message.edit_text(
                t("payments.external_ready", lang),
                reply_markup=build_stars_payment_method_keyboard(
                    order_id=str(order.id),
                    wallet_payment_url=build_wallet_payment_url_for_payload(wallet_bot_username, order.payload),
                    lang=lang,
                ),
            )
        else:
            await send_stars_invoice(callback.message, order, lang)
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
        await callback.answer(t("payments.invoice_error", lang), show_alert=True)


@router.callback_query(lambda cb: cb.data.startswith("pay:invoice:"))
async def pay_stars_invoice_here(callback: CallbackQuery, session: AsyncSession) -> None:
    """Fallback button: pay an existing Stars order via sendInvoice in this bot."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    raw_order_id = callback.data.removeprefix("pay:invoice:") if callback.data else ""
    try:
        order_id = UUID(raw_order_id)
    except ValueError:
        await callback.answer(t("payments.invoice_error", lang), show_alert=True)
        return

    order = await PaymentRepository(session).get_payment_order_by_id(order_id)
    if order is None or order.user_id != callback.from_user.id or order.provider != PaymentProvider.TELEGRAM_STARS.value:
        logger.warning(
            {
                "action": "telegram_stars_invoice_order_not_found",
                "user_id": callback.from_user.id,
                "order_id": raw_order_id,
                "order_found": order is not None,
            }
        )
        await callback.answer(t("payments.invoice_error", lang), show_alert=True)
        return

    await send_stars_invoice(callback.message, order, lang)
    await callback.answer()


@router.pre_checkout_query()
async def process_stars_pre_checkout(pre_checkout_query: PreCheckoutQuery, session: AsyncSession) -> None:
    """Validate Telegram Stars pre-checkout queries."""
    lang = get_user_language(getattr(pre_checkout_query.from_user, "language_code", None))
    order = await PaymentRepository(session).get_payment_order_by_payload(pre_checkout_query.invoice_payload)
    if (
        order is not None
        and order.provider == PaymentProvider.TELEGRAM_STARS.value
        and order.amount == pre_checkout_query.total_amount
    ):
        await pre_checkout_query.answer(ok=True)
        return

    logger.warning(
        {
            "action": "telegram_stars_pre_checkout_rejected",
            "user_id": pre_checkout_query.from_user.id,
            "total_amount": pre_checkout_query.total_amount,
            "order_found": order is not None,
        }
    )
    await pre_checkout_query.answer(ok=False, error_message=t("payments.pre_checkout_failed", lang))


@router.message(lambda message: getattr(message, "successful_payment", None) is not None)
async def process_successful_stars_payment(message: Message, session: AsyncSession) -> None:
    """Complete a Telegram Stars payment after successful_payment."""
    lang = get_user_language(getattr(message.from_user, "language_code", None))
    successful_payment = message.successful_payment
    try:
        order = await PaymentService(session).complete_stars_payment(
            payload=successful_payment.invoice_payload,
            telegram_payment_charge_id=successful_payment.telegram_payment_charge_id,
            total_amount=successful_payment.total_amount,
        )
        user = await UserRepository(session).get_by_id(order.user_id)
        balance = user.balance if user is not None else order.credits
        await message.answer(t("payments.received", lang, credits=order.credits, balance=balance))
    except PaymentOrderNotFoundError as exc:
        logger.warning(
            {
                "action": "telegram_stars_payment_order_not_found",
                "user_id": getattr(message.from_user, "id", None),
                "error": exc.__class__.__name__,
            }
        )
        await message.answer(t("payments.complete_error", lang))
    except Exception as exc:
        logger.exception(
            {
                "action": "telegram_stars_payment_complete_error",
                "user_id": getattr(message.from_user, "id", None),
                "error": exc.__class__.__name__,
            }
        )
        await message.answer(t("payments.complete_error", lang))


@router.callback_query(lambda cb: cb.data.startswith("pay:crypto:"))
async def reject_crypto_payment_callbacks(callback: CallbackQuery) -> None:
    """Keep crafted crypto callbacks as coming-soon until a trusted provider is connected."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    await callback.answer(t("payments.crypto_coming_soon", lang), show_alert=True)
