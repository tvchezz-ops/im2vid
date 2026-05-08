from __future__ import annotations

import os
import re

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.db.base import Base
from app.db.models import PaymentOrderStatus, PaymentProvider, User
from app.services.payments import ALLOWED_STARS_AMOUNTS, PaymentService


EXPECTED_STARS_AMOUNTS = (100, 300, 500, 1000, 3000, 5000)


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "payment-service.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_stars_order_uses_one_star_to_one_credit(session_factory, caplog) -> None:
    async with session_factory() as session:
        session.add(User(id=201, balance=0))
        await session.commit()
        service = PaymentService(session)

        order = await service.create_stars_order(user_id=201, stars_amount=300)

        assert order.provider == PaymentProvider.TELEGRAM_STARS.value
        assert order.amount == 300
        assert order.credits == 300
        assert order.currency == "XTR"
        assert order.payload is not None
        assert order.payload.startswith("stars_")
        assert len(order.payload) <= 64
        assert re.fullmatch(r"[A-Za-z0-9_-]+", order.payload)
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "payment_order_created"
            and "telegram_payment_charge_id" not in record.msg
            for record in caplog.records
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stars_amount", ALLOWED_STARS_AMOUNTS)
async def test_create_stars_order_accepts_only_allowed_amounts(session_factory, stars_amount: int) -> None:
    assert ALLOWED_STARS_AMOUNTS == EXPECTED_STARS_AMOUNTS

    async with session_factory() as session:
        session.add(User(id=220 + stars_amount, balance=0))
        await session.commit()
        service = PaymentService(session)

        order = await service.create_stars_order(user_id=220 + stars_amount, stars_amount=stars_amount)

        assert order.amount == stars_amount
        assert order.credits == stars_amount
        assert order.currency == "XTR"


@pytest.mark.asyncio
async def test_create_stars_order_generates_unique_payloads(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=221, balance=0))
        await session.commit()
        service = PaymentService(session)

        orders = [await service.create_stars_order(user_id=221, stars_amount=100) for _ in range(5)]

        payloads = [order.payload for order in orders]
        assert len(payloads) == len(set(payloads))
        assert all(payload is not None and payload.startswith("stars_") and len(payload) <= 64 for payload in payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("stars_amount", [0, 99, 101, 250, 299, 301, 499, 501, 999, 1001, 2999, 3001, 4999, 5001])
async def test_create_stars_order_rejects_unsupported_amount(session_factory, stars_amount: int) -> None:
    async with session_factory() as session:
        service = PaymentService(session)

        with pytest.raises(ValueError):
            await service.create_stars_order(user_id=202, stars_amount=stars_amount)


@pytest.mark.asyncio
async def test_complete_stars_payment_credits_user_once(session_factory, caplog) -> None:
    async with session_factory() as session:
        session.add(User(id=203, balance=7))
        await session.commit()
        service = PaymentService(session)
        order = await service.create_stars_order(user_id=203, stars_amount=500)

        paid_order = await service.complete_stars_payment(
            payload=order.payload,
            telegram_payment_charge_id="charge-203",
            total_amount=500,
        )
        repeated_order = await service.complete_stars_payment(
            payload=order.payload,
            telegram_payment_charge_id="duplicate-charge-203",
            total_amount=500,
        )

        result = await session.execute(select(User.balance).where(User.id == 203))
        assert result.scalar_one() == 507
        assert paid_order.status == PaymentOrderStatus.PAID.value
        assert paid_order.telegram_payment_charge_id == "charge-203"
        assert repeated_order.telegram_payment_charge_id == "charge-203"
        credit_logs = [
            record.msg
            for record in caplog.records
            if isinstance(record.msg, dict) and record.msg.get("action") == "credits_added"
        ]
        assert len(credit_logs) == 1
        payment_logs = [record.msg for record in caplog.records if isinstance(record.msg, dict)]
        assert any(log.get("action") == "payment_paid" and log.get("order_id") == str(order.id) for log in payment_logs)
        assert all("order_id" in log for log in payment_logs if log.get("action") in {"payment_paid", "credits_added"})
        assert all("payload" not in log for log in payment_logs)
        assert all("telegram_payment_charge_id" not in log for log in payment_logs)
        assert all(order.payload not in str(log) for log in payment_logs)
        assert all("charge-203" not in str(log) for log in payment_logs)


@pytest.mark.asyncio
async def test_complete_stars_payment_returns_paid_order_without_recrediting(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=207, balance=2))
        await session.commit()
        service = PaymentService(session)
        order = await service.create_stars_order(user_id=207, stars_amount=100)
        await service.complete_stars_payment(
            payload=order.payload,
            telegram_payment_charge_id="charge-207",
            total_amount=100,
        )

        repeated_order = await service.complete_stars_payment(
            payload=order.payload,
            telegram_payment_charge_id="duplicate-charge-207",
            total_amount=300,
        )

        result = await session.execute(select(User.balance).where(User.id == 207))
        assert result.scalar_one() == 102
        assert repeated_order.status == PaymentOrderStatus.PAID.value
        assert repeated_order.telegram_payment_charge_id == "charge-207"


@pytest.mark.asyncio
async def test_complete_stars_payment_rejects_amount_mismatch(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=204, balance=0))
        await session.commit()
        service = PaymentService(session)
        order = await service.create_stars_order(user_id=204, stars_amount=100)

        with pytest.raises(ValueError):
            await service.complete_stars_payment(
                payload=order.payload,
                telegram_payment_charge_id="charge-204",
                total_amount=300,
            )

        result = await session.execute(select(User.balance).where(User.id == 204))
        assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_mark_external_stars_payment_paid_credits_user_once(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=206, balance=4))
        await session.commit()
        service = PaymentService(session)
        order = await service.create_stars_order(user_id=206, stars_amount=100)

        paid_order = await service.mark_external_stars_payment_paid(
            payload=order.payload,
            external_payment_id="wallet-payment-206",
        )
        repeated_order = await service.mark_external_stars_payment_paid(
            payload=order.payload,
            external_payment_id="wallet-payment-duplicate",
        )

        result = await session.execute(select(User.balance).where(User.id == 206))
        assert result.scalar_one() == 104
        assert paid_order.status == PaymentOrderStatus.PAID.value
        assert paid_order.external_payment_id == "wallet-payment-206"
        assert repeated_order.external_payment_id == "wallet-payment-206"

