"""NOWPayments API integration."""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import PaymentProvider, PaymentRepository
from app.services.crypto_payments import CryptoInvoice, CryptoPaymentProvider, CryptoPaymentStatus
from app.utils import logger


class NowPaymentsService:
    """Small async client for NOWPayments payments and IPN verification."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None):
        self.client = client

    @property
    def base_url(self) -> str:
        return settings.nowpayments_base_url.strip().rstrip("/")

    @property
    def api_base_url(self) -> str:
        base_url = self.base_url
        return base_url if base_url.endswith("/v1") else f"{base_url}/v1"

    @property
    def return_url(self) -> str:
        if not settings.main_bot_username.strip():
            logger.warning({"action": "nowpayments_main_bot_username_missing", "fallback_url": "https://t.me"})
            return "https://t.me"
        return settings.main_bot_url

    @property
    def ipn_callback_url(self) -> str:
        configured_url = settings.nowpayments_ipn_callback_url.strip()
        if configured_url:
            return configured_url
        return f"{settings.public_base_url.strip().rstrip('/')}/webhooks/nowpayments"

    async def create_payment(
        self,
        *,
        order_id: str,
        credits: int,
        amount_usd: Decimal | float | str,
        pay_currency: Optional[str] = None,
    ) -> dict[str, Any]:
        return_url = self.return_url
        payload: dict[str, Any] = {
            "price_amount": str(amount_usd),
            "price_currency": "usd",
            "order_id": str(order_id),
            "order_description": f"Top up {credits} credits",
            "ipn_callback_url": self.ipn_callback_url,
            "success_url": return_url,
            "cancel_url": return_url,
        }
        if pay_currency:
            payload["pay_currency"] = pay_currency

        headers = {
            "x-api-key": settings.nowpayments_api_key.strip(),
            "Content-Type": "application/json",
        }
        if self.client is not None:
            response = await self.client.post(f"{self.api_base_url}/payment", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{self.api_base_url}/payment", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    async def get_payment_status(self, payment_id: str) -> dict[str, Any]:
        headers = {"x-api-key": settings.nowpayments_api_key.strip()}
        if self.client is not None:
            response = await self.client.get(f"{self.api_base_url}/payment/{payment_id}", headers=headers)
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{self.api_base_url}/payment/{payment_id}", headers=headers)
            response.raise_for_status()
            return response.json()

    def verify_ipn_signature(self, raw_body: bytes, signature: str) -> bool:
        if not signature or not settings.nowpayments_ipn_secret.strip():
            return False
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False

        canonical_body = json.dumps(body, sort_keys=True, separators=(",", ":"))
        expected_signature = hmac.new(
            settings.nowpayments_ipn_secret.strip().encode("utf-8"),
            canonical_body.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected_signature, signature)


class NOWPaymentsProvider(CryptoPaymentProvider):
    """Crypto payment provider backed by NOWPayments."""

    def __init__(self, session: AsyncSession, *, service: Optional[NowPaymentsService] = None):
        self.session = session
        self.payment_repo = PaymentRepository(session)
        self.service = service or NowPaymentsService()

    async def create_invoice(
        self,
        user_id: int,
        amount_credits: int,
        asset: str,
        network: str,
    ) -> CryptoInvoice:
        if amount_credits <= 0:
            raise ValueError("Crypto invoice amount must be positive")

        price_amount = _credits_to_usd(amount_credits)
        order = await self.payment_repo.create_payment_order(
            user_id=user_id,
            provider=PaymentProvider.CRYPTO.value,
            amount=amount_credits,
            credits=amount_credits,
            currency="USD",
            metadata={"provider": "nowpayments"},
        )
        await self.payment_repo.create_crypto_payment_order(
            order.id,
            asset=asset.strip().upper() or None,
            network=network.strip().upper() or None,
            price_amount=price_amount,
            price_currency="usd",
            status="draft",
        )
        payment = await self.service.create_payment(
            order_id=str(order.id),
            credits=amount_credits,
            amount_usd=price_amount,
            pay_currency=_build_pay_currency(asset, network),
        )
        payment_id = str(payment.get("payment_id") or payment.get("paymentID") or "").strip()
        pay_address = str(payment.get("pay_address") or "").strip()
        pay_amount = str(payment.get("pay_amount") or payment.get("actually_paid") or "").strip()
        pay_currency = str(payment.get("pay_currency") or "").strip()
        invoice_url = payment.get("invoice_url") or payment.get("payment_url")
        if not payment_id or not pay_address or not pay_currency:
            raise ValueError("NOWPayments response missing required payment fields")

        parsed_asset, parsed_network = _parse_pay_currency(pay_currency)
        await self.payment_repo.attach_nowpayments_payment_details(
            order.id,
            nowpayments_payment_id=payment_id,
            payment_url=str(invoice_url) if invoice_url else None,
            pay_address=pay_address,
            pay_currency=pay_currency,
            price_amount=price_amount,
            price_currency="usd",
            status="pending",
        )
        crypto_order = await self.payment_repo.get_crypto_payment_order_by_payment_order_id(order.id)
        if crypto_order is not None:
            crypto_order.asset = parsed_asset
            crypto_order.network = parsed_network
            crypto_order.wallet_address = pay_address
            crypto_order.expected_amount = pay_amount or price_amount
            await self.session.commit()

        logger.info({"action": "nowpayments_payment_created", "order_id": str(order.id), "payment_id": payment_id})
        return CryptoInvoice(
            invoice_id=payment_id,
            asset=parsed_asset,
            network=parsed_network,
            amount=pay_amount or price_amount,
            address=pay_address,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            payment_url=str(invoice_url) if invoice_url else None,
        )

    async def verify_payment(self, invoice_id: str) -> CryptoPaymentStatus:
        payment = await self.service.get_payment_status(invoice_id)
        payment_status = str(payment.get("payment_status") or "").lower()
        status = "paid" if payment_status == "finished" else "pending"
        if payment_status in {"failed", "expired", "refunded"}:
            status = "failed" if payment_status != "expired" else "expired"
        tx_hash = payment.get("payin_hash") or payment.get("outcome_hash") or payment.get("tx_hash")
        paid_amount = payment.get("actually_paid") or payment.get("pay_amount")
        return CryptoPaymentStatus(
            status=status,
            tx_hash=str(tx_hash) if tx_hash else None,
            paid_amount=str(paid_amount) if paid_amount else None,
        )


def _credits_to_usd(credits: int) -> str:
    return f"{Decimal(credits) * Decimal('0.01'):.2f}"


def _build_pay_currency(asset: str, network: str) -> Optional[str]:
    asset_value = asset.strip().lower()
    network_value = network.strip().lower()
    if not asset_value:
        return None
    if asset_value == "usdt" and network_value in {"trc20", "tron"}:
        return "usdttrc20"
    if asset_value == "usdt" and network_value in {"erc20", "ethereum"}:
        return "usdterc20"
    return asset_value


def _parse_pay_currency(pay_currency: str) -> tuple[str, str]:
    normalized = pay_currency.strip().lower()
    if normalized == "usdttrc20":
        return "USDT", "TRC20"
    if normalized == "usdterc20":
        return "USDT", "ERC20"
    if normalized == "btc":
        return "BTC", "BTC"
    if normalized == "eth":
        return "ETH", "ERC20"
    return normalized.upper(), normalized.upper()
