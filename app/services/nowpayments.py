"""NOWPayments API integration."""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any, Optional

import httpx

from app.config import settings
from app.utils import logger


class NowPaymentsService:
    """Small async client for NOWPayments payments and IPN verification."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None):
        self.client = client

    @property
    def base_url(self) -> str:
        return settings.nowpayments_base_url.strip().rstrip("/")

    @property
    def return_url(self) -> str:
        if not settings.main_bot_username.strip():
            logger.warning({"action": "nowpayments_main_bot_username_missing", "fallback_url": "https://t.me"})
            return "https://t.me"
        return settings.main_bot_url

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
            "ipn_callback_url": settings.nowpayments_ipn_callback_url.strip(),
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
            response = await self.client.post(f"{self.base_url}/payment", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{self.base_url}/payment", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    async def get_payment_status(self, payment_id: str) -> dict[str, Any]:
        headers = {"x-api-key": settings.nowpayments_api_key.strip()}
        if self.client is not None:
            response = await self.client.get(f"{self.base_url}/payment/{payment_id}", headers=headers)
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{self.base_url}/payment/{payment_id}", headers=headers)
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
