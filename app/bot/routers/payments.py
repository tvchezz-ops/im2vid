"""Payment top-up router."""
from __future__ import annotations

from uuid import UUID

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import build_stars_payment_method_keyboard, build_stars_top_up_keyboard, get_profile_keyboard
from app.bot.routers.profile import build_profile_text
from app.config import settings
from app.db import UserRepository
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


def build_wallet_payment_url(wallet_bot_username: str, payload: str) -> str:
    """Build a Telegram deep link to an external wallet bot."""
    return f"https://t.me/{_normalize_bot_username(wallet_bot_username)}?start={payload}"


def build_wallet_return_url(payload: str) -> str | None:
    """Build the optional return deep link from an external wallet bot."""
    bot_username = _normalize_bot_username(settings.telegram_stars_return_bot_username)
    if not bot_username:
        return None
    return f"https://t.me/{bot_username}?start=paid_{payload}"


async def _send_stars_invoice(message: Message, order, lang: str) -> None:
    await message.answer_invoice(
        title=t("payments.invoice_title", lang),
        description=t("payments.invoice_description", lang, amount=order.amount),
        payload=order.payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=t("payments.invoice_label", lang, amount=order.amount), amount=order.amount)],
    )


@router.callback_query(lambda cb: cb.data.startswith("pay:stars:"))
async def choose_stars_amount(callback: CallbackQuery, session: AsyncSession) -> None:
    """Create a Stars payment order and open the configured payment path."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    amount = _parse_stars_amount(callback.data)
    if amount not in ALLOWED_STARS_AMOUNTS:
        await callback.answer(t("payments.invalid_amount", lang), show_alert=True)
        return

    try:
        payment_service = PaymentService(session)
        order = await payment_service.create_stars_order(callback.from_user.id, amount)
        wallet_bot_username = _normalize_bot_username(settings.telegram_stars_wallet_bot_username)
        if wallet_bot_username:
            await callback.message.edit_text(
                t("payments.choose_method", lang),
                reply_markup=build_stars_payment_method_keyboard(
                    order_id=str(order.id),
                    wallet_payment_url=build_wallet_payment_url(wallet_bot_username, order.payload),
                    lang=lang,
                ),
            )
        else:
            await _send_stars_invoice(callback.message, order, lang)
        await callback.answer()
    except Exception as exc:
        logger.exception(
            {
                "action": "payment_invoice_error",
                "user_id": callback.from_user.id,
                "amount": amount,
                "error": exc.__class__.__name__,
            }
        )
        await callback.answer(t("payments.invoice_error", lang), show_alert=True)


@router.callback_query(lambda cb: cb.data.startswith("pay:invoice:"))
async def pay_stars_invoice_fallback(callback: CallbackQuery, session: AsyncSession) -> None:
    """Send an in-bot Stars invoice for an existing payment order."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    order_id = callback.data.removeprefix("pay:invoice:") if callback.data else ""
    try:
        payment_repo = PaymentService(session).payment_repo
        order = await payment_repo.get_payment_order_by_id(UUID(order_id))
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer(t("payments.pre_checkout_failed", lang), show_alert=True)
            return
        await _send_stars_invoice(callback.message, order, lang)
        await callback.answer()
    except Exception as exc:
        logger.exception(
            {
                "action": "payment_invoice_error",
                "user_id": callback.from_user.id,
                "error": exc.__class__.__name__,
            }
        )
        await callback.answer(t("payments.invoice_error", lang), show_alert=True)


@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery, session: AsyncSession) -> None:
    """Validate a Telegram Stars pre-checkout query."""
    try:
        payment_repo = PaymentService(session).payment_repo
        order = await payment_repo.get_payment_order_by_payload(pre_checkout_query.invoice_payload)
        if order is not None and order.amount == pre_checkout_query.total_amount:
            await pre_checkout_query.answer(ok=True)
            return
    except Exception as exc:
        logger.exception(
            {
                "action": "payment_pre_checkout_error",
                "user_id": pre_checkout_query.from_user.id,
                "error": exc.__class__.__name__,
            }
        )

    lang = get_user_language(getattr(pre_checkout_query.from_user, "language_code", None))
    await pre_checkout_query.answer(ok=False, error_message=t("payments.pre_checkout_failed", lang))


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, session: AsyncSession) -> None:
    """Complete a successful Telegram Stars payment."""
    lang = get_user_language(getattr(message.from_user, "language_code", None))
    successful_payment = message.successful_payment
    try:
        payment_service = PaymentService(session)
        order = await payment_service.complete_stars_payment(
            payload=successful_payment.invoice_payload,
            telegram_payment_charge_id=successful_payment.telegram_payment_charge_id,
            total_amount=successful_payment.total_amount,
        )
        user = await UserRepository(session).get_user_profile(order.user_id)
        balance = user.balance if user is not None else 0
        await message.answer(t("payments.success", lang, credits=order.credits, balance=balance))
    except (PaymentOrderNotFoundError, ValueError) as exc:
        logger.warning(
            {
                "action": "payment_complete_rejected",
                "user_id": getattr(message.from_user, "id", None),
                "error": exc.__class__.__name__,
            }
        )
        await message.answer(t("payments.failed", lang))
    except Exception as exc:
        logger.exception(
            {
                "action": "payment_complete_error",
                "user_id": getattr(message.from_user, "id", None),
                "error": exc.__class__.__name__,
            }
        )
        await message.answer(t("payments.failed", lang))
