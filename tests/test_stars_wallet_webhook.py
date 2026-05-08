from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.config import settings
from app.db.base import Base
from app.db.models import PaymentOrder, PaymentOrderStatus, User
from app.services.payments import PaymentService
from app.services.telegram_files import DB_SESSION_FACTORY_APP_KEY, TELEGRAM_BOT_APP_KEY, create_media_app
from app.services.stars_wallet_webhook import process_stars_wallet_webhook


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "stars-wallet-webhook.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


async def _create_stars_order(session_factory, *, user_id: int = 920, balance: int = 10, amount: int = 100):
    async with session_factory() as session:
        session.add(User(id=user_id, balance=balance))
        await session.commit()
        order = await PaymentService(session).create_stars_order(user_id=user_id, stars_amount=amount)
        return order.id, order.payload


def _raw_payload(*, payload: str, amount: int = 100, user_id: int = 920, payment_id: str = "wallet-payment-920") -> bytes:
    return json.dumps(
        {
            "payload": payload,
            "amount": amount,
            "telegram_user_id": user_id,
            "external_payment_id": payment_id,
        }
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_stars_wallet_webhook_rejects_wrong_secret(session_factory, monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "telegram_stars_webhook_secret", "secret")
    caplog.set_level("INFO")
    order_id, payload = await _create_stars_order(session_factory)

    status, response = await process_stars_wallet_webhook(
        raw_body=_raw_payload(payload=payload),
        webhook_secret="wrong",
        session_factory=session_factory,
    )

    async with session_factory() as session:
        balance = (await session.execute(select(User.balance).where(User.id == 920))).scalar_one()
        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))).scalar_one()

    assert status == 403
    assert response == {"ok": False, "error": "invalid_secret"}
    assert balance == 10
    assert order.status == PaymentOrderStatus.CREATED.value
    assert any(
        isinstance(record.msg, dict)
        and record.msg.get("action") == "stars_wallet_webhook_rejected"
        and record.msg.get("reason") == "invalid_secret"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_stars_wallet_webhook_paid_credits_user_and_saves_external_payment_id(session_factory, monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "telegram_stars_webhook_secret", "secret")
    caplog.set_level("INFO")
    order_id, payload = await _create_stars_order(session_factory, user_id=921, balance=7, amount=300)
    bot = FakeBot()

    status, response = await process_stars_wallet_webhook(
        raw_body=_raw_payload(payload=payload, amount=300, user_id=921, payment_id="wallet-payment-921"),
        webhook_secret="secret",
        session_factory=session_factory,
        bot=bot,
    )

    async with session_factory() as session:
        balance = (await session.execute(select(User.balance).where(User.id == 921))).scalar_one()
        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))).scalar_one()

    assert status == 200
    assert response == {"ok": True}
    assert balance == 307
    assert order.status == PaymentOrderStatus.PAID.value
    assert order.external_payment_id == "wallet-payment-921"
    assert order.paid_at is not None
    assert bot.messages == [(921, "✅ Оплата получена. Начислено 300 кредитов.")]
    actions = [record.msg.get("action") for record in caplog.records if isinstance(record.msg, dict)]
    assert "stars_wallet_webhook_received" in actions
    assert "stars_wallet_payment_confirmed" in actions


@pytest.mark.asyncio
async def test_stars_wallet_webhook_repeated_paid_does_not_credit_twice(session_factory, monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "telegram_stars_webhook_secret", "secret")
    caplog.set_level("INFO")
    order_id, payload = await _create_stars_order(session_factory, user_id=922, balance=1, amount=100)
    bot = FakeBot()
    raw_body = _raw_payload(payload=payload, amount=100, user_id=922, payment_id="wallet-payment-922")

    first_status, first_response = await process_stars_wallet_webhook(
        raw_body=raw_body,
        webhook_secret="secret",
        session_factory=session_factory,
        bot=bot,
    )
    second_status, second_response = await process_stars_wallet_webhook(
        raw_body=raw_body,
        webhook_secret="secret",
        session_factory=session_factory,
        bot=bot,
    )

    async with session_factory() as session:
        balance = (await session.execute(select(User.balance).where(User.id == 922))).scalar_one()
        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))).scalar_one()

    assert first_status == 200
    assert first_response == {"ok": True}
    assert second_status == 200
    assert second_response == {"ok": True, "already_paid": True}
    assert balance == 101
    assert order.external_payment_id == "wallet-payment-922"
    assert len(bot.messages) == 1
    duplicate_logs = [record.msg for record in caplog.records if isinstance(record.msg, dict) and record.msg.get("action") == "stars_wallet_payment_duplicate"]
    assert len(duplicate_logs) == 1


@pytest.mark.asyncio
async def test_stars_wallet_webhook_rejects_amount_mismatch_without_crediting(session_factory, monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "telegram_stars_webhook_secret", "secret")
    caplog.set_level("INFO")
    order_id, payload = await _create_stars_order(session_factory, user_id=923, balance=5, amount=500)

    status, response = await process_stars_wallet_webhook(
        raw_body=_raw_payload(payload=payload, amount=100, user_id=923, payment_id="wallet-payment-923"),
        webhook_secret="secret",
        session_factory=session_factory,
    )

    async with session_factory() as session:
        balance = (await session.execute(select(User.balance).where(User.id == 923))).scalar_one()
        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))).scalar_one()

    assert status == 400
    assert response == {"ok": False, "error": "amount_mismatch"}
    assert balance == 5
    assert order.status == PaymentOrderStatus.CREATED.value
    assert any(
        isinstance(record.msg, dict)
        and record.msg.get("action") == "stars_wallet_webhook_rejected"
        and record.msg.get("reason") == "amount_mismatch"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_stars_wallet_webhook_route_accepts_secret_header(session_factory, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "telegram_stars_webhook_secret", "secret")
    monkeypatch.setattr(settings, "temp_media_dir", str(tmp_path))
    order_id, payload = await _create_stars_order(session_factory, user_id=924, balance=11, amount=100)
    bot = FakeBot()
    app = create_media_app(bot)
    app[DB_SESSION_FACTORY_APP_KEY] = session_factory
    app[TELEGRAM_BOT_APP_KEY] = bot
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        response = await client.post(
            "/webhooks/stars-wallet",
            data=_raw_payload(payload=payload, amount=100, user_id=924, payment_id="wallet-payment-924"),
            headers={"X-Webhook-Secret": "secret", "Content-Type": "application/json"},
        )
        body = await response.json()
    finally:
        await client.close()

    async with session_factory() as session:
        balance = (await session.execute(select(User.balance).where(User.id == 924))).scalar_one()
        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))).scalar_one()

    assert response.status == 200
    assert body == {"ok": True}
    assert balance == 111
    assert order.status == PaymentOrderStatus.PAID.value
    assert bot.messages == [(924, "✅ Оплата получена. Начислено 100 кредитов.")]
