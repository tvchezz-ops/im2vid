"""NOWPayments checkout-link integration."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import PaymentOrder, PaymentOrderStatus, PaymentProvider, PaymentRepository
from app.utils import logger


@dataclass(frozen=True)
class NowPaymentsOrderLink:
    """Checkout link created by NOWPayments for a payment order."""

    order: PaymentOrder
    payment_url: str
    price_amount: str
    payment_id: str | None = None


class NowPaymentsService:
    """Small async client for NOWPayments invoices and IPN verification."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None, session: Optional[AsyncSession] = None):
        self.client = client
        self.session = session
        self.payment_repo = PaymentRepository(session) if session is not None else None

    @property
    def base_url(self) -> str:
        return settings.nowpayments_base_url.strip().rstrip("/")

    @property
    def api_base_url(self) -> str:
        base_url = self.base_url
        return base_url if base_url.endswith("/v1") else f"{base_url}/v1"

    @property
    def ipn_callback_url(self) -> str:
        return f"{settings.public_base_url.strip().rstrip('/')}/webhooks/nowpayments"

    async def create_payment_order_link(self, user_id: int, credits: int) -> NowPaymentsOrderLink:
        """Create a local payment order and a NOWPayments checkout URL."""
        if self.payment_repo is None:
            raise RuntimeError("create_payment_order_link requires a database session")
        if credits <= 0:
            raise ValueError("Credits amount must be positive")

        price_amount = _credits_to_usd(credits)
        order = await self.payment_repo.create_payment_order(
            user_id=user_id,
            provider=PaymentProvider.NOWPAYMENTS.value,
            amount=credits,
            credits=credits,
            currency="USD",
            metadata={
                "provider": "nowpayments",
                "price_amount": price_amount,
                "price_currency": "usd",
            },
        )
        response = await self.create_invoice(
            order_id=str(order.id),
            credits=credits,
            price_amount=price_amount,
        )
        payment_url = _extract_payment_url(response)
        payment_id = _extract_payment_id(response)
        metadata = {
            "provider": "nowpayments",
            "price_amount": price_amount,
            "price_currency": "usd",
            "payment_url": payment_url,
            "nowpayments_response": _sanitize_for_log(response),
        }
        updated_order = await self.payment_repo.update_nowpayments_order_metadata(
            order.id,
            payment_id=payment_id,
            status=PaymentOrderStatus.PENDING.value,
            metadata=metadata,
        )
        if updated_order is None:
            raise ValueError("Payment order not found")
        logger.info(
            {
                "action": "nowpayments_order_link_created",
                "order_id": str(updated_order.id),
                "user_id": user_id,
                "credits": credits,
                "price_amount": price_amount,
                "has_payment_url": bool(payment_url),
            }
        )
        return NowPaymentsOrderLink(
            order=updated_order,
            payment_url=payment_url,
            price_amount=price_amount,
            payment_id=payment_id,
        )

    async def create_invoice(self, *, order_id: str, credits: int, price_amount: Decimal | float | str) -> dict[str, Any]:
        """Create a NOWPayments invoice/checkout page without choosing currency for the user."""
        normalized_price_amount = _normalize_price_amount(price_amount)
        payload: dict[str, Any] = {
            "price_amount": normalized_price_amount,
            "price_currency": "usd",
            "order_id": str(order_id),
            "order_description": f"{credits} IMai credits",
            "ipn_callback_url": self.ipn_callback_url,
        }
        if settings.nowpayments_success_url.strip():
            payload["success_url"] = settings.nowpayments_success_url.strip()
        if settings.nowpayments_cancel_url.strip():
            payload["cancel_url"] = settings.nowpayments_cancel_url.strip()

        headers = {
            "x-api-key": settings.nowpayments_api_key.strip(),
            "Content-Type": "application/json",
        }
        if self.client is not None:
            response = await self.client.post(f"{self.api_base_url}/invoice", json=payload, headers=headers)
            _log_nowpayments_error_response(response, payload)
            response.raise_for_status()
            return response.json()

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{self.api_base_url}/invoice", json=payload, headers=headers)
            _log_nowpayments_error_response(response, payload)
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

    async def handle_ipn(self, payload: dict[str, Any]) -> None:
        """Compatibility hook; IPN processing lives in app.services.nowpayments_webhook."""
        logger.info({"action": "nowpayments_handle_ipn_received", "payload_keys": sorted(payload)})


def _credits_to_usd(credits: int) -> str:
    amount = (Decimal(credits) * settings.credit_usd_price).quantize(Decimal("0.01"))
    return f"{amount:.2f}"


def _normalize_price_amount(amount_usd: Decimal | float | str) -> float:
    amount = Decimal(str(amount_usd))
    if amount <= 0:
        raise ValueError("NOWPayments price_amount must be greater than zero")
    return float(amount)


def _extract_payment_id(response: dict[str, Any]) -> str | None:
    payment_id = response.get("payment_id") or response.get("paymentID") or response.get("id") or response.get("invoice_id")
    return str(payment_id).strip() if payment_id else None


def _extract_payment_url(response: dict[str, Any]) -> str:
    payment_url = response.get("invoice_url") or response.get("payment_url") or response.get("url")
    if not payment_url:
        raise ValueError("NOWPayments response missing payment_url/invoice_url")
    return str(payment_url)


def _sanitize_for_log(value: Any) -> Any:
    if isinstance(value, str):
        sanitized_value = value
        for secret_value in (settings.nowpayments_api_key.strip(), settings.nowpayments_ipn_secret.strip()):
            if secret_value:
                sanitized_value = sanitized_value.replace(secret_value, "[redacted]")
        return sanitized_value
    if isinstance(value, dict):
        sanitized = {}
        for key, nested_value in value.items():
            normalized_key = str(key).lower()
            if any(secret_marker in normalized_key for secret_marker in ("key", "secret", "token", "authorization", "signature")):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_for_log(nested_value)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_log(item) for item in value]
    return value


def _get_response_body_for_log(response: httpx.Response) -> Any:
    try:
        return _sanitize_for_log(response.json())
    except Exception:
        return _sanitize_for_log(getattr(response, "text", ""))


def _log_nowpayments_error_response(response: httpx.Response, payload: dict[str, Any]) -> None:
    status_code = getattr(response, "status_code", 200)
    if status_code < 400:
        return
    logger.error(
        {
            "action": "nowpayments_create_invoice_error_response",
            "status_code": status_code,
            "response_body": _get_response_body_for_log(response),
            "payload_keys": sorted(payload),
        }
    )
