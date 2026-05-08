"""Crypto payment provider abstractions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, Protocol, Union

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import PaymentProvider, PaymentRepository


CryptoPaymentState = Literal["pending", "paid", "expired", "failed"]


@dataclass(frozen=True)
class CryptoInvoice:
    invoice_id: str
    asset: str
    network: str
    amount: Union[int, str]
    address: str
    expires_at: datetime
    payment_url: Optional[str] = None


@dataclass(frozen=True)
class CryptoPaymentStatus:
    status: CryptoPaymentState
    tx_hash: Optional[str] = None
    paid_amount: Optional[str] = None


class CryptoPaymentProvider(Protocol):
    async def create_invoice(
        self,
        user_id: int,
        amount_credits: int,
        asset: str,
        network: str,
    ) -> CryptoInvoice:
        """Create a crypto payment invoice."""

    async def verify_payment(self, invoice_id: str) -> CryptoPaymentStatus:
        """Verify a crypto payment invoice."""


class StubCryptoPaymentProvider:
    """Draft-only crypto provider used until a real provider/webhook is connected."""

    def __init__(self, session: AsyncSession, *, invoice_ttl_minutes: int = 30):
        self.session = session
        self.payment_repo = PaymentRepository(session)
        self.invoice_ttl_minutes = invoice_ttl_minutes

    async def create_invoice(
        self,
        user_id: int,
        amount_credits: int,
        asset: str,
        network: str,
    ) -> CryptoInvoice:
        if amount_credits <= 0:
            raise ValueError("Crypto invoice amount must be positive")

        normalized_asset = asset.strip().upper()
        normalized_network = network.strip().upper()
        order = await self.payment_repo.create_payment_order(
            user_id=user_id,
            provider=PaymentProvider.CRYPTO.value,
            amount=amount_credits,
            credits=amount_credits,
            currency=normalized_asset or "CRYPTO",
            metadata={"network": normalized_network},
        )
        await self.payment_repo.create_crypto_payment_order(
            order.id,
            asset=normalized_asset,
            network=normalized_network,
            wallet_address="",
            expected_amount=str(amount_credits),
            status="draft",
        )

        return CryptoInvoice(
            invoice_id=str(order.id),
            asset=normalized_asset,
            network=normalized_network,
            amount=amount_credits,
            address="",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=self.invoice_ttl_minutes),
            payment_url=None,
        )

    async def verify_payment(self, invoice_id: str) -> CryptoPaymentStatus:
        return CryptoPaymentStatus(status="pending")
