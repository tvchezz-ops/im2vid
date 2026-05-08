from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.config import settings
from app.services.nowpayments import NowPaymentsService


class FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeClient:
    def __init__(self):
        self.posts: list[dict[str, object]] = []
        self.gets: list[dict[str, object]] = []

    async def post(self, url: str, json: dict[str, object], headers: dict[str, str]) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(
            {
                "payment_id": "np-1",
                "invoice_url": "https://nowpayments.test/pay/np-1",
                "pay_address": "wallet-address",
                "pay_amount": "1.00",
                "pay_currency": "usdttrc20",
                "order_id": "order-1",
            }
        )

    async def get(self, url: str, headers: dict[str, str]) -> FakeResponse:
        self.gets.append({"url": url, "headers": headers})
        return FakeResponse({"payment_id": "np-1", "payment_status": "waiting"})


@pytest.mark.asyncio
async def test_create_payment_sends_callback_url_and_api_headers(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_api_key", "api-key")
    monkeypatch.setattr(settings, "nowpayments_base_url", "https://api.nowpayments.test")
    monkeypatch.setattr(settings, "nowpayments_ipn_callback_url", "https://bot.test/webhooks/nowpayments")
    monkeypatch.setattr(settings, "main_bot_username", "main_bot")
    client = FakeClient()

    response = await NowPaymentsService(client=client).create_payment(
        order_id="order-1",
        credits=100,
        amount_usd="1.00",
        pay_currency="usdttrc20",
    )

    assert response["payment_id"] == "np-1"
    assert client.posts[0]["url"] == "https://api.nowpayments.test/v1/payment"
    assert client.posts[0]["headers"] == {"x-api-key": "api-key", "Content-Type": "application/json"}
    payload = client.posts[0]["json"]
    assert payload["price_amount"] == "1.00"
    assert payload["price_currency"] == "usd"
    assert payload["pay_currency"] == "usdttrc20"
    assert payload["order_id"] == "order-1"
    assert payload["ipn_callback_url"] == "https://bot.test/webhooks/nowpayments"
    assert payload["success_url"] == "https://t.me/main_bot"
    assert payload["cancel_url"] == "https://t.me/main_bot"


@pytest.mark.asyncio
async def test_create_payment_uses_tme_fallback_without_main_bot_username(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_api_key", "api-key")
    monkeypatch.setattr(settings, "nowpayments_base_url", "https://api.nowpayments.test")
    monkeypatch.setattr(settings, "nowpayments_ipn_callback_url", "https://bot.test/webhooks/nowpayments")
    monkeypatch.setattr(settings, "main_bot_username", "")
    client = FakeClient()

    await NowPaymentsService(client=client).create_payment(
        order_id="order-1",
        credits=100,
        amount_usd="1.00",
    )

    payload = client.posts[0]["json"]
    assert payload["success_url"] == "https://t.me"
    assert payload["cancel_url"] == "https://t.me"


def _sign(payload: dict[str, object], secret: str) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512).hexdigest()


def test_verify_ipn_signature_accepts_sorted_compact_json(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_ipn_secret", "ipn-secret")
    payload = {"payment_status": "finished", "payment_id": "np-1", "order_id": "order-1"}
    raw_body = json.dumps(payload, indent=2).encode("utf-8")

    assert NowPaymentsService().verify_ipn_signature(raw_body, _sign(payload, "ipn-secret")) is True


def test_verify_ipn_signature_rejects_invalid_signature(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_ipn_secret", "ipn-secret")
    payload = {"payment_status": "finished", "payment_id": "np-1"}

    assert NowPaymentsService().verify_ipn_signature(json.dumps(payload).encode("utf-8"), "bad") is False
