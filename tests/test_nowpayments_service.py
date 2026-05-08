from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest
import httpx

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.config import settings
from app.services.nowpayments import NowPaymentsService


class FakeResponse:
    def __init__(self, payload: dict[str, object], *, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.nowpayments.test/v1/invoice")
            response = httpx.Response(self.status_code, request=request, json=self.payload)
            raise httpx.HTTPStatusError("bad request", request=request, response=response)
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeClient:
    def __init__(self, response: FakeResponse | None = None):
        self.posts: list[dict[str, object]] = []
        self.gets: list[dict[str, object]] = []
        self.response = response

    async def post(self, url: str, json: dict[str, object], headers: dict[str, str]) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers})
        if self.response is not None:
            return self.response
        return FakeResponse(
            {
                "payment_id": "np-1",
                "invoice_url": "https://nowpayments.test/pay/np-1",
                "payment_status": "waiting",
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
    monkeypatch.setattr(settings, "public_base_url", "https://bot.test")
    monkeypatch.setattr(settings, "nowpayments_success_url", "https://bot.test/success")
    monkeypatch.setattr(settings, "nowpayments_cancel_url", "https://bot.test/cancel")
    client = FakeClient()

    response = await NowPaymentsService(client=client).create_invoice(
        order_id="order-1",
        credits=100,
        price_amount="1.00",
    )

    assert response["payment_id"] == "np-1"
    assert client.posts[0]["url"] == "https://api.nowpayments.test/v1/invoice"
    assert client.posts[0]["headers"] == {"x-api-key": "api-key", "Content-Type": "application/json"}
    payload = client.posts[0]["json"]
    assert payload["price_amount"] == 1.0
    assert payload["price_currency"] == "usd"
    assert payload["order_id"] == "order-1"
    assert payload["order_description"] == "100 IMai credits"
    assert payload["ipn_callback_url"] == "https://bot.test/webhooks/nowpayments"
    assert payload["success_url"] == "https://bot.test/success"
    assert payload["cancel_url"] == "https://bot.test/cancel"
    assert set(payload) == {
        "price_amount",
        "price_currency",
        "order_id",
        "order_description",
        "ipn_callback_url",
        "success_url",
        "cancel_url",
    }


@pytest.mark.asyncio
async def test_create_payment_uses_tme_fallback_without_main_bot_username(monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_api_key", "api-key")
    monkeypatch.setattr(settings, "nowpayments_base_url", "https://api.nowpayments.test")
    monkeypatch.setattr(settings, "public_base_url", "https://bot.test/")
    monkeypatch.setattr(settings, "nowpayments_success_url", "")
    monkeypatch.setattr(settings, "nowpayments_cancel_url", "")
    client = FakeClient()

    await NowPaymentsService(client=client).create_invoice(
        order_id="order-1",
        credits=100,
        price_amount="1.00",
    )

    payload = client.posts[0]["json"]
    assert payload["ipn_callback_url"] == "https://bot.test/webhooks/nowpayments"
    assert "success_url" not in payload
    assert "cancel_url" not in payload


@pytest.mark.asyncio
async def test_create_payment_logs_sanitized_400_response(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "nowpayments_api_key", "api-key-secret")
    monkeypatch.setattr(settings, "nowpayments_base_url", "https://api.nowpayments.test")
    monkeypatch.setattr(settings, "public_base_url", "https://bot.test")
    monkeypatch.setattr(settings, "nowpayments_success_url", "")
    monkeypatch.setattr(settings, "nowpayments_cancel_url", "")
    caplog.set_level("ERROR")
    client = FakeClient(
        FakeResponse(
            {
                "message": "Bad Request",
                "api_key": "api-key-secret",
                "details": {"token": "secret-token", "field": "price_amount"},
            },
            status_code=400,
        )
    )

    with pytest.raises(httpx.HTTPStatusError):
        await NowPaymentsService(client=client).create_invoice(
            order_id="order-400",
            credits=100,
            price_amount="1.00",
        )

    error_logs = [record.msg for record in caplog.records if isinstance(record.msg, dict) and record.msg.get("action") == "nowpayments_create_invoice_error_response"]
    assert error_logs == [
        {
            "action": "nowpayments_create_invoice_error_response",
            "status_code": 400,
            "response_body": {
                "message": "Bad Request",
                "api_key": "[redacted]",
                "details": {"token": "[redacted]", "field": "price_amount"},
            },
            "payload_keys": [
                "ipn_callback_url",
                "order_description",
                "order_id",
                "price_amount",
                "price_currency",
            ],
        }
    ]
    assert "api-key-secret" not in str(error_logs)


@pytest.mark.asyncio
async def test_create_payment_requires_positive_price_amount(monkeypatch) -> None:
    with pytest.raises(ValueError, match="price_amount"):
        await NowPaymentsService(client=FakeClient()).create_invoice(
            order_id="order-zero",
            credits=100,
            price_amount="0",
        )


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
