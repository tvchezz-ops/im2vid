"""Payment service orchestration."""
from __future__ import annotations

import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import PaymentOrder, PaymentOrderStatus, PaymentProvider, PaymentRepository
from app.utils import logger


ALLOWED_STARS_AMOUNTS = (100, 300, 500, 1000, 3000, 5000)


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

        order = await self.payment_repo.create_payment_order(
            user_id=user_id,
            provider=PaymentProvider.TELEGRAM_STARS.value,
            amount=stars_amount,
            credits=stars_amount,
            currency="XTR",
            payload=None,
            metadata={},
        )

        order.payload = f"stars_{order.id.hex}_{secrets.token_urlsafe(12)}"
        await self.session.commit()
        await self.session.refresh(order)

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
            return order

        if total_amount != order.amount:
            raise ValueError("Telegram Stars payment amount mismatch")

        was_paid = order.status == PaymentOrderStatus.PAID.value
        paid_order = await self.payment_repo.mark_payment_order_paid(
            order.id,
            telegram_payment_charge_id=telegram_payment_charge_id,
        )
        if paid_order is None:
            raise PaymentOrderNotFoundError("Payment order not found")
        self._log_payment_paid(paid_order)
        if not was_paid:
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

    async def create_crypto_draft_order(
        self,
        user_id: int,
        amount: int,
        asset: str | None = None,
        network: str | None = None,
    ) -> PaymentOrder:
        """Create a draft crypto payment order."""
        if amount <= 0:
            raise ValueError("Crypto payment amount must be positive")

        order = await self.payment_repo.create_payment_order(
            user_id=user_id,
            provider=PaymentProvider.CRYPTO.value,
            amount=amount,
            credits=amount,
            currency=asset or "CRYPTO",
            metadata={},
        )
        await self.payment_repo.create_crypto_payment_order(
            order.id,
            asset=asset,
            network=network,
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
