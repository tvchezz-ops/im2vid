"""NOWPayments IPN webhook handling."""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db import PaymentOrderStatus, PaymentRepository, UserRepository
from app.i18n import get_user_language, t
from app.services.nowpayments import NowPaymentsService
from app.utils import logger


NOWPAYMENTS_WEBHOOK_PATH = "/webhooks/nowpayments"
NOWPAYMENTS_PAID_STATUSES = {"finished", "confirmed"}
NOWPAYMENTS_FAILED_STATUSES = {"failed", "expired"}
NOWPAYMENTS_PENDING_STATUSES = {"waiting", "confirming", "sending"}
NOWPAYMENTS_IPN_METADATA_KEYS = (
    "payment_id",
    "payment_status",
    "order_id",
    "pay_amount",
    "actually_paid",
    "outcome_amount",
    "price_amount",
    "price_currency",
)


async def process_nowpayments_ipn(
    *,
    raw_body: bytes,
    signature: str,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Optional[Bot] = None,
    service: Optional[NowPaymentsService] = None,
) -> tuple[int, dict[str, object]]:
    nowpayments = service or NowPaymentsService()
    if not nowpayments.verify_ipn_signature(raw_body, signature):
        reason = "ipn_secret_not_configured" if not settings.nowpayments_ipn_secret.strip() else "invalid_signature"
        logger.error({"action": "nowpayments_ipn_rejected", "reason": reason})
        return 403, {"ok": False, "error": reason}

    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.info({"action": "nowpayments_ipn_rejected", "reason": "invalid_json"})
        return 400, {"ok": False, "error": "invalid_json"}

    payment_id = str(payload.get("payment_id") or payload.get("paymentID") or "").strip()
    payment_status = str(payload.get("payment_status") or "").strip().lower()
    order_id = str(payload.get("order_id") or "").strip()
    if not payment_id or not payment_status or not order_id:
        logger.info(
            {
                "action": "nowpayments_ipn_rejected",
                "reason": "missing_payment_fields",
                "payment_id": payment_id,
                "status": payment_status,
            }
        )
        return 400, {"ok": False, "error": "missing_payment_fields"}
    try:
        order_uuid = uuid.UUID(order_id)
    except ValueError:
        logger.warning(
            {
                "action": "nowpayments_ipn_rejected",
                "reason": "invalid_order_id",
                "payment_id": payment_id,
                "status": payment_status,
                "order_id": order_id,
            }
        )
        return 404, {"ok": False, "error": "order_not_found"}

    metadata = {"last_ipn": _build_nowpayments_ipn_metadata(payload)}
    logger.info(
        {
            "action": "nowpayments_ipn_received",
            "payment_id": payment_id,
            "status": payment_status,
            "order_id": order_id,
        }
    )
    async with session_factory() as session:
        payment_repo = PaymentRepository(session)
        order = await payment_repo.get_payment_order_by_id(order_uuid)
        if order is None:
            logger.warning(
                {
                    "action": "nowpayments_ipn_rejected",
                    "reason": "order_not_found",
                    "payment_id": payment_id,
                    "status": payment_status,
                    "order_id": order_id,
                }
            )
            return 404, {"ok": False, "error": "order_not_found"}

        await payment_repo.update_nowpayments_order_metadata(
            order.id,
            payment_id=payment_id,
            status=payment_status,
            metadata=metadata,
        )

        if order.status == PaymentOrderStatus.PAID.value:
            logger.info(
                {
                    "action": "nowpayments_duplicate_ignored",
                    "payment_id": payment_id,
                    "order_id": str(order.id),
                    "user_id": order.user_id,
                    "status": payment_status,
                }
            )
            return 200, {"ok": True, "already_paid": True}

        if payment_status in NOWPAYMENTS_PAID_STATUSES:
            completion = await payment_repo.complete_nowpayments_payment_and_credit_user(
                order.id,
                payment_id=payment_id,
                status=payment_status,
            )
            if completion.order is not None and completion.already_paid:
                logger.info(
                    {
                        "action": "nowpayments_duplicate_ignored",
                        "payment_id": payment_id,
                        "order_id": str(completion.order.id),
                        "user_id": completion.order.user_id,
                        "status": payment_status,
                    }
                )
                return 200, {"ok": True, "already_paid": True}
            if completion.order is None:
                logger.warning(
                    {
                        "action": "nowpayments_ipn_rejected",
                        "reason": "order_not_found",
                        "payment_id": payment_id,
                        "status": payment_status,
                        "order_id": order_id,
                    }
                )
                return 404, {"ok": False, "error": "order_not_found"}

            logger.info(
                {
                    "action": "nowpayments_payment_paid",
                    "payment_id": payment_id,
                    "order_id": str(completion.order.id),
                    "user_id": completion.order.user_id,
                    "credits": completion.order.credits,
                    "status": payment_status,
                }
            )
            if bot is not None:
                user = await UserRepository(session).get_by_id(completion.order.user_id)
                lang = get_user_language(user.language_code if user is not None else None)
                await bot.send_message(
                    completion.order.user_id,
                    t("payments.crypto_received", lang, credits=completion.order.credits),
                )
            return 200, {"ok": True, "status": "paid"}

        if payment_status in NOWPAYMENTS_FAILED_STATUSES:
            status = PaymentOrderStatus.EXPIRED.value if payment_status == "expired" else PaymentOrderStatus.FAILED.value
            await payment_repo.update_nowpayments_order_metadata(
                order.id,
                payment_id=payment_id,
                status=status,
                metadata=metadata,
            )
            logger.info(
                {
                    "action": "nowpayments_payment_failed",
                    "payment_id": payment_id,
                    "order_id": str(order.id),
                    "user_id": order.user_id,
                    "status": payment_status,
                }
            )
            return 200, {"ok": True, "status": payment_status}

        if payment_status in NOWPAYMENTS_PENDING_STATUSES:
            await payment_repo.update_nowpayments_order_metadata(
                order.id,
                payment_id=payment_id,
                status=payment_status,
                metadata=metadata,
            )
            logger.info(
                {
                    "action": "nowpayments_payment_pending",
                    "payment_id": payment_id,
                    "order_id": str(order.id),
                    "user_id": order.user_id,
                    "status": payment_status,
                }
            )
            return 200, {"ok": True, "status": "pending"}

        await payment_repo.update_nowpayments_order_metadata(
            order.id,
            payment_id=payment_id,
            status=payment_status,
            metadata=metadata,
        )
        logger.info(
            {
                "action": "nowpayments_payment_pending",
                "payment_id": payment_id,
                "order_id": str(order.id),
                "user_id": order.user_id,
                "status": payment_status,
            }
        )
        return 200, {"ok": True, "status": "ignored"}


def _build_nowpayments_ipn_metadata(payload: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(payload[key])
        for key in NOWPAYMENTS_IPN_METADATA_KEYS
        if key in payload and payload[key] is not None
    }
