from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.config import settings
from app.db.base import Base
from app.db.models import PaymentOrderStatus, User
from app.db.repositories import PaymentRepository
from app.services.nowpayments_webhook import process_nowpayments_ipn


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "nowpayments-webhook.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


def _raw_and_signature(payload: dict[str, object], secret: str = "ipn-secret") -> tuple[bytes, str]:
    raw_body = json.dumps(payload).encode("utf-8")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512).hexdigest()
    return raw_body, signature


async def _create_nowpayments_order(session_factory, *, user_id: int = 501, payment_id: str = "np-501"):
    async with session_factory() as session:
        session.add(User(id=user_id, balance=7))
        await session.commit()
        repo = PaymentRepository(session)
        order = await repo.create_payment_order(
            user_id=user_id,
            provider="crypto",
            amount=100,
            credits=100,
            currency="USD",
        )
        await repo.create_crypto_payment_order(
            order.id,
            nowpayments_payment_id=payment_id,
            payment_url="https://nowpayments.test/pay",
            price_amount="1.00",
            status="pending",
        )
        await repo.attach_nowpayments_payment_details(
            order.id,
            nowpayments_payment_id=payment_id,
            payment_url="https://nowpayments.test/pay",
            price_amount="1.00",
            status="pending",
        )
        return order.id


@pytest.mark.asyncio
async def test_nowpayments_webhook_rejects_invalid_signature(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_ipn_secret", "ipn-secret")
    raw_body = json.dumps({"payment_id": "np-1", "payment_status": "finished"}).encode("utf-8")

    status, payload = await process_nowpayments_ipn(
        raw_body=raw_body,
        signature="bad",
        session_factory=session_factory,
    )

    assert status == 401
    assert payload == {"status": "invalid_signature"}


@pytest.mark.asyncio
async def test_nowpayments_finished_webhook_credits_user_once(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_ipn_secret", "ipn-secret")
    await _create_nowpayments_order(session_factory, user_id=502, payment_id="np-502")
    bot = FakeBot()
    raw_body, signature = _raw_and_signature({"payment_id": "np-502", "payment_status": "finished", "payin_hash": "tx-502"})

    first_status, first_payload = await process_nowpayments_ipn(
        raw_body=raw_body,
        signature=signature,
        session_factory=session_factory,
        bot=bot,
    )
    second_status, second_payload = await process_nowpayments_ipn(
        raw_body=raw_body,
        signature=signature,
        session_factory=session_factory,
        bot=bot,
    )

    async with session_factory() as session:
        balance = (await session.execute(select(User.balance).where(User.id == 502))).scalar_one()

    assert first_status == 200
    assert first_payload == {"status": "paid"}
    assert second_status == 200
    assert second_payload == {"status": "already_paid"}
    assert balance == 107
    assert len(bot.messages) == 1


@pytest.mark.asyncio
async def test_nowpayments_pending_webhook_does_not_credit(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_ipn_secret", "ipn-secret")
    await _create_nowpayments_order(session_factory, user_id=503, payment_id="np-503")
    raw_body, signature = _raw_and_signature({"payment_id": "np-503", "payment_status": "confirming"})

    status, payload = await process_nowpayments_ipn(
        raw_body=raw_body,
        signature=signature,
        session_factory=session_factory,
    )

    async with session_factory() as session:
        balance = (await session.execute(select(User.balance).where(User.id == 503))).scalar_one()

    assert status == 200
    assert payload == {"status": "pending"}
    assert balance == 7


@pytest.mark.asyncio
async def test_nowpayments_failed_webhook_does_not_credit(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "nowpayments_ipn_secret", "ipn-secret")
    order_id = await _create_nowpayments_order(session_factory, user_id=504, payment_id="np-504")
    raw_body, signature = _raw_and_signature({"payment_id": "np-504", "payment_status": "expired"})

    status, payload = await process_nowpayments_ipn(
        raw_body=raw_body,
        signature=signature,
        session_factory=session_factory,
    )

    async with session_factory() as session:
        balance = (await session.execute(select(User.balance).where(User.id == 504))).scalar_one()
        order = await PaymentRepository(session).get_payment_order_by_id(order_id)

    assert status == 200
    assert payload == {"status": "failed"}
    assert balance == 7
    assert order is not None
    assert order.status == PaymentOrderStatus.FAILED.value
