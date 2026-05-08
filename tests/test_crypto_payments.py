from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.config import Settings, settings
from app.db.base import Base
from app.db.models import PaymentOrder, PaymentOrderStatus, PaymentProvider, User
from app.services.nowpayments import NowPaymentsService


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeClient:
    def __init__(self):
        self.posts: list[dict[str, object]] = []

    async def post(self, url: str, json: dict[str, object], headers: dict[str, str]) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(
            {
                "payment_id": "np-checkout-1",
                "invoice_url": "https://nowpayments.test/invoice/np-checkout-1",
                "order_id": json["order_id"],
            }
        )


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "crypto-payments.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credits", "expected_price_amount", "expected_payload_amount"),
    [
        (100, "1.30", 1.3),
        (300, "3.90", 3.9),
        (1000, "13.00", 13.0),
        (5000, "65.00", 65.0),
    ],
)
async def test_nowpayments_checkout_link_uses_configured_credit_price(
    session_factory,
    monkeypatch,
    credits: int,
    expected_price_amount: str,
    expected_payload_amount: float,
) -> None:
    monkeypatch.setattr(settings, "nowpayments_api_key", "api-key")
    monkeypatch.setattr(settings, "nowpayments_base_url", "https://api.nowpayments.test")
    monkeypatch.setattr(settings, "public_base_url", "https://bot.test")
    monkeypatch.setattr(settings, "nowpayments_success_url", "")
    monkeypatch.setattr(settings, "nowpayments_cancel_url", "")
    monkeypatch.setattr(settings, "credit_usd_price", Decimal("0.013"))
    client = FakeClient()

    async with session_factory() as session:
        user_id = 300000 + credits
        session.add(User(id=user_id, balance=0))
        await session.commit()

        payment_link = await NowPaymentsService(client=client, session=session).create_payment_order_link(
            user_id=user_id,
            credits=credits,
        )

        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == user_id))).scalar_one()

    assert payment_link.payment_url == "https://nowpayments.test/invoice/np-checkout-1"
    assert payment_link.price_amount == expected_price_amount
    assert order.provider == PaymentProvider.NOWPAYMENTS.value
    assert order.status == PaymentOrderStatus.PENDING.value
    assert order.amount == credits
    assert order.credits == credits
    assert order.currency == "USD"
    assert order.nowpayments_payment_id == "np-checkout-1"
    assert order.metadata_["payment_url"] == "https://nowpayments.test/invoice/np-checkout-1"
    assert order.metadata_["price_amount"] == expected_price_amount
    assert "wallet_address" not in order.metadata_
    assert "network" not in order.metadata_
    assert "tx_hash" not in order.metadata_

    payload = client.posts[0]["json"]
    assert client.posts[0]["url"] == "https://api.nowpayments.test/v1/invoice"
    assert payload["price_amount"] == expected_payload_amount
    assert payload["price_currency"] == "usd"
    assert payload["order_id"] == str(order.id)
    assert payload["order_description"] == f"{credits} IMai credits"
    assert payload["ipn_callback_url"] == "https://bot.test/webhooks/nowpayments"
    assert "pay_currency" not in payload


@pytest.mark.asyncio
async def test_nowpayments_checkout_link_rejects_invalid_credit_amount(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(ValueError, match="Credits amount"):
            await NowPaymentsService(client=FakeClient(), session=session).create_payment_order_link(
                user_id=302,
                credits=0,
            )


def test_credit_usd_price_defaults_to_nowpayments_crypto_price(monkeypatch) -> None:
    monkeypatch.delenv("CREDIT_USD_PRICE", raising=False)

    loaded_settings = Settings(
        bot_token="test-bot-token",
        wavespeed_api_key="test-api-key",
        public_base_url="https://example.com",
        _env_file=None,
    )

    assert loaded_settings.credit_usd_price == Decimal("0.013")
