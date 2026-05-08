"""Verified callbacks from the external Telegram Stars wallet bot."""
from __future__ import annotations

import json
from typing import Any, Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db import PaymentOrderStatus, PaymentProvider, PaymentRepository
from app.utils import logger


STARS_WALLET_WEBHOOK_PATH = "/webhooks/stars-wallet"


async def process_stars_wallet_webhook(
    *,
    raw_body: bytes,
    webhook_secret: str,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Optional[Bot] = None,
) -> tuple[int, dict[str, str]]:
    """Verify and apply a trusted Stars wallet payment notification."""
    if not settings.telegram_stars_webhook_secret.strip() or not webhook_secret:
        return 401, {"status": "invalid_secret"}
    if webhook_secret != settings.telegram_stars_webhook_secret.strip():
        return 401, {"status": "invalid_secret"}

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"status": "invalid_json"}

    payment_payload = str(payload.get("payload") or "").strip()
    external_payment_id = str(payload.get("external_payment_id") or "").strip()
    try:
        amount = int(payload.get("amount"))
        telegram_user_id = int(payload.get("telegram_user_id"))
    except (TypeError, ValueError):
        return 400, {"status": "invalid_fields"}
    if not payment_payload or not external_payment_id:
        return 400, {"status": "invalid_fields"}

    logger.info(
        {
            "action": "wallet_webhook_received",
            "payment_id": external_payment_id,
            "amount": amount,
            "telegram_user_id": telegram_user_id,
        }
    )
    async with session_factory() as session:
        payment_repo = PaymentRepository(session)
        order = await payment_repo.get_payment_order_by_payload(payment_payload)
        if order is None or order.provider != PaymentProvider.TELEGRAM_STARS.value:
            return 404, {"status": "order_not_found"}
        if order.user_id != telegram_user_id:
            return 400, {"status": "user_mismatch"}
        if order.amount != amount:
            return 400, {"status": "amount_mismatch"}
        if order.status == PaymentOrderStatus.PAID.value:
            return 200, {"status": "already_paid"}

        paid_order = await payment_repo.mark_payment_order_paid(order.id, external_payment_id=external_payment_id)
        if paid_order is None:
            return 404, {"status": "order_not_found"}
        if bot is not None:
            await bot.send_message(order.user_id, f"✅ Оплата получена. Начислено {amount} кредитов.")
        return 200, {"status": "paid"}