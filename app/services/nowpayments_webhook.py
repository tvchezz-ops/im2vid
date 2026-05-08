"""NOWPayments IPN webhook handling."""
from __future__ import annotations

import json
from typing import Any, Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import PaymentOrderStatus, PaymentRepository, UserRepository
from app.i18n import get_user_language, t
from app.services.nowpayments import NowPaymentsService
from app.utils import logger


NOWPAYMENTS_WEBHOOK_PATH = "/webhooks/nowpayments"
NOWPAYMENTS_PAID_STATUSES = {"finished"}
NOWPAYMENTS_FAILED_STATUSES = {"failed", "expired", "refunded"}
NOWPAYMENTS_PENDING_STATUSES = {"waiting", "confirming", "sending", "partially_paid"}


async def process_nowpayments_ipn(
    *,
    raw_body: bytes,
    signature: str,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Optional[Bot] = None,
    service: Optional[NowPaymentsService] = None,
) -> tuple[int, dict[str, str]]:
    nowpayments = service or NowPaymentsService()
    if not nowpayments.verify_ipn_signature(raw_body, signature):
        logger.warning({"action": "nowpayments_ipn_invalid_signature"})
        return 401, {"status": "invalid_signature"}

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"status": "invalid_json"}

    payment_id = str(payload.get("payment_id") or payload.get("paymentID") or "").strip()
    payment_status = str(payload.get("payment_status") or "").strip().lower()
    tx_hash = payload.get("payin_hash") or payload.get("outcome_hash") or payload.get("tx_hash")
    if not payment_id or not payment_status:
        return 400, {"status": "missing_payment_fields"}

    logger.info({"action": "nowpayments_ipn_received", "payment_id": payment_id, "status": payment_status})
    async with session_factory() as session:
        payment_repo = PaymentRepository(session)
        crypto_order = await payment_repo.get_crypto_payment_order_by_nowpayments_id(payment_id)
        if crypto_order is None:
            logger.warning({"action": "nowpayments_order_not_found", "payment_id": payment_id, "status": payment_status})
            return 200, {"status": "order_not_found"}
        order = await payment_repo.get_payment_order_by_id(crypto_order.payment_order_id)
        if order is None:
            return 200, {"status": "order_not_found"}
        if order.status == PaymentOrderStatus.PAID.value:
            return 200, {"status": "already_paid"}

        if payment_status in NOWPAYMENTS_PAID_STATUSES:
            completion = await payment_repo.complete_nowpayments_payment_and_credit_user(
                payment_id,
                tx_hash=str(tx_hash) if tx_hash else None,
                status=payment_status,
            )
            if completion.order is not None and not completion.already_paid and bot is not None:
                user = await UserRepository(session).get_by_id(completion.order.user_id)
                lang = get_user_language(user.language_code if user is not None else None)
                balance = user.balance if user is not None else completion.order.credits
                await bot.send_message(
                    completion.order.user_id,
                    t("payments.received", lang, credits=completion.order.credits, balance=balance),
                )
            return 200, {"status": "paid"}

        if payment_status in NOWPAYMENTS_FAILED_STATUSES:
            await payment_repo.mark_nowpayments_order_failed(payment_id, payment_status, str(tx_hash) if tx_hash else None)
            return 200, {"status": "failed"}

        if payment_status in NOWPAYMENTS_PENDING_STATUSES:
            crypto_order.status = payment_status
            await session.commit()
            return 200, {"status": "pending"}

    return 200, {"status": "ignored"}
