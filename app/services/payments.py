"""Payment service orchestration."""
from __future__ import annotations

import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import PaymentOrder, PaymentOrderStatus, PaymentProvider, PaymentRepository
from app.utils import logger


ALLOWED_STARS_AMOUNTS = (100, 300, 500, 1000, 3000, 5000)
TELEGRAM_DEEP_LINK_PAYLOAD_LIMIT = 64


class PaymentOrderNotFoundError(ValueError):
    """Raised when a payment order cannot be found."""


class PaymentService:
    """High-level payment order workflows."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.payment_repo = PaymentRepository(session)

    async def create_stars_order(self, user_id: int, stars_amount: int) -> PaymentOrder:
        """Create a Telegram Stars payment order."""
        if stars_amount not in ALLOWED_STARS_AMOUNTS:
            raise ValueError("Unsupported Telegram Stars amount")

        payload = f"stars_{secrets.token_urlsafe(24)}"
        if len(payload) > TELEGRAM_DEEP_LINK_PAYLOAD_LIMIT:
            raise ValueError("Telegram Stars payload is too long")

        order = await self.payment_repo.create_payment_order(
            user_id=user_id,
            provider=PaymentProvider.TELEGRAM_STARS.value,
            amount=stars_amount,
            credits=stars_amount,
            currency="XTR",
            payload=payload,
            metadata={},
        )

        logger.info(
            {
                "action": "payment_order_created",
                "order_id": str(order.id),
                "user_id": user_id,
                "provider": order.provider,
                "amount": order.amount,
                "credits": order.credits,
                "currency": order.currency,
            }
        )

        return order

    async def complete_stars_payment(
        self,
        payload: str,
        telegram_payment_charge_id: str,
        total_amount: int,
    ) -> PaymentOrder:
        """Complete a Telegram Stars payment and credit the user once."""
        order = await self.payment_repo.get_payment_order_by_payload(payload)
        if order is None:
            raise PaymentOrderNotFoundError("Payment order not found")

        if order.provider != PaymentProvider.TELEGRAM_STARS.value:
            raise ValueError("Payment order provider is not Telegram Stars")

        if order.status == PaymentOrderStatus.PAID.value:
            self._log_payment_paid(order)
            return order

        if total_amount != order.amount:
            raise ValueError("Telegram Stars payment amount mismatch")

        result = await self.payment_repo.complete_payment_and_credit_user(
            payload,
            telegram_payment_charge_id=telegram_payment_charge_id,
            total_amount=total_amount,
        )
        paid_order = result.order
        if paid_order is None:
            raise PaymentOrderNotFoundError("Payment order not found")
        self._log_payment_paid(paid_order)
        if not result.already_paid:
            self._log_credits_added(paid_order)
        return paid_order

    async def mark_external_stars_payment_paid(
        self,
        payload: str,
        external_payment_id: str,
    ) -> PaymentOrder:
        """Mark a trusted external Stars wallet payment paid and credit once."""
        if not external_payment_id.strip():
            raise ValueError("External payment id is required")

        order = await self.payment_repo.get_payment_order_by_payload(payload)
        if order is None:
            raise PaymentOrderNotFoundError("Payment order not found")

        if order.provider != PaymentProvider.TELEGRAM_STARS.value:
            raise ValueError("Payment order provider is not Telegram Stars")

        if order.status == PaymentOrderStatus.PAID.value:
            return order

        paid_order = await self.payment_repo.mark_payment_order_paid(
            order.id,
            external_payment_id=external_payment_id,
        )
        if paid_order is None:
            raise PaymentOrderNotFoundError("Payment order not found")

        self._log_payment_paid(paid_order)
        self._log_credits_added(paid_order)
        return paid_order

    async def credit_user_for_paid_order(self, order: PaymentOrder) -> None:
        """Mark an order paid and credit its user idempotently."""
        was_paid = order.status == PaymentOrderStatus.PAID.value
        paid_order = await self.payment_repo.mark_payment_order_paid(order.id)
        if paid_order is None:
            raise PaymentOrderNotFoundError("Payment order not found")

        self._log_payment_paid(paid_order)
        if not was_paid:
            self._log_credits_added(paid_order)

    @staticmethod
    def _log_payment_paid(order: PaymentOrder) -> None:
        logger.info(
            {
                "action": "payment_paid",
                "order_id": str(order.id),
                "user_id": order.user_id,
                "provider": order.provider,
                "amount": order.amount,
                "credits": order.credits,
                "currency": order.currency,
            }
        )

    @staticmethod
    def _log_credits_added(order: PaymentOrder) -> None:
        logger.info(
            {
                "action": "credits_added",
                "order_id": str(order.id),
                "user_id": order.user_id,
                "credits": order.credits,
            }
        )
