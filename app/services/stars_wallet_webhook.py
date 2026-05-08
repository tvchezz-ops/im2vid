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
) -> tuple[int, dict[str, object]]:
    """Verify and apply a trusted Stars wallet payment notification."""
    if not settings.telegram_stars_webhook_secret.strip() or not webhook_secret:
        logger.info({"action": "stars_wallet_webhook_rejected", "reason": "invalid_secret"})
        return 403, {"ok": False, "error": "invalid_secret"}
    if webhook_secret != settings.telegram_stars_webhook_secret.strip():
        logger.info({"action": "stars_wallet_webhook_rejected", "reason": "invalid_secret"})
        return 403, {"ok": False, "error": "invalid_secret"}

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.info({"action": "stars_wallet_webhook_rejected", "reason": "invalid_json"})
        return 400, {"ok": False, "error": "invalid_json"}

    payment_payload = str(payload.get("payload") or "").strip()
    external_payment_id = str(payload.get("external_payment_id") or "").strip()
    try:
        amount = int(payload.get("amount"))
        telegram_user_id = int(payload.get("telegram_user_id"))
    except (TypeError, ValueError):
        logger.info({"action": "stars_wallet_webhook_rejected", "reason": "invalid_fields"})
        return 400, {"ok": False, "error": "invalid_fields"}
    if not payment_payload or not external_payment_id:
        logger.info({"action": "stars_wallet_webhook_rejected", "reason": "invalid_fields"})
        return 400, {"ok": False, "error": "invalid_fields"}

    logger.info(
        {
            "action": "stars_wallet_webhook_received",
            "amount": amount,
            "telegram_user_id": telegram_user_id,
        }
    )
    async with session_factory() as session:
        payment_repo = PaymentRepository(session)
        order = await payment_repo.get_payment_order_by_payload(payment_payload)
        if order is None:
            logger.info(
                {
                    "action": "stars_wallet_webhook_rejected",
                    "reason": "order_not_found",
                    "amount": amount,
                    "telegram_user_id": telegram_user_id,
                }
            )
            return 404, {"ok": False, "error": "order_not_found"}
        if order.provider != PaymentProvider.TELEGRAM_STARS.value:
            logger.info(
                {
                    "action": "stars_wallet_webhook_rejected",
                    "reason": "provider_mismatch",
                    "order_id": str(order.id),
                    "provider": order.provider,
                    "telegram_user_id": telegram_user_id,
                }
            )
            return 400, {"ok": False, "error": "provider_mismatch"}
        if order.user_id != telegram_user_id:
            logger.info(
                {
                    "action": "stars_wallet_webhook_rejected",
                    "reason": "user_mismatch",
                    "order_id": str(order.id),
                    "telegram_user_id": telegram_user_id,
                }
            )
            return 400, {"ok": False, "error": "user_mismatch"}
        if order.amount != amount:
            logger.info(
                {
                    "action": "stars_wallet_webhook_rejected",
                    "reason": "amount_mismatch",
                    "order_id": str(order.id),
                    "amount": amount,
                    "expected_amount": order.amount,
                    "telegram_user_id": telegram_user_id,
                }
            )
            return 400, {"ok": False, "error": "amount_mismatch"}
        if order.status == PaymentOrderStatus.PAID.value:
            logger.info(
                {
                    "action": "stars_wallet_payment_duplicate",
                    "order_id": str(order.id),
                    "user_id": order.user_id,
                    "amount": order.amount,
                }
            )
            return 200, {"ok": True, "already_paid": True}

        paid_order = await payment_repo.mark_payment_order_paid(order.id, external_payment_id=external_payment_id)
        if paid_order is None:
            logger.info(
                {
                    "action": "stars_wallet_webhook_rejected",
                    "reason": "order_not_found",
                    "amount": amount,
                    "telegram_user_id": telegram_user_id,
                }
            )
            return 404, {"ok": False, "error": "order_not_found"}
        logger.info(
            {
                "action": "stars_wallet_payment_confirmed",
                "order_id": str(paid_order.id),
                "user_id": paid_order.user_id,
                "amount": paid_order.amount,
                "credits": paid_order.credits,
            }
        )
        if bot is not None:
            await bot.send_message(paid_order.user_id, f"✅ Оплата получена. Начислено {paid_order.credits} кредитов.")
        return 200, {"ok": True}