from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.db.base import Base
from app.db.models import PaymentOrderStatus, User
from app.db.repositories import PaymentRepository


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "payment-repository.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


@pytest.mark.asyncio
async def test_create_payment_order_and_get_by_payload(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=101, balance=0))
        await session.commit()
        repo = PaymentRepository(session)

        order = await repo.create_payment_order(
            user_id=101,
            provider="telegram_stars",
            amount=250,
            credits=25,
            currency="XTR",
            payload="stars-invoice-101",
            metadata={"package": "small"},
        )
        found = await repo.get_payment_order_by_payload("stars-invoice-101")

        assert found is not None
        assert found.id == order.id
        assert found.status == PaymentOrderStatus.CREATED.value
        assert found.metadata_ == {"package": "small"}


@pytest.mark.asyncio
async def test_payment_order_payload_is_unique(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=107, balance=0))
        await session.commit()
        repo = PaymentRepository(session)
        await repo.create_payment_order(
            user_id=107,
            provider="telegram_stars_wallet_bot",
            amount=100,
            credits=100,
            currency="XTR",
            payload="unique-wallet-payload",
        )

        with pytest.raises(IntegrityError):
            await repo.create_payment_order(
                user_id=107,
                provider="telegram_stars_wallet_bot",
                amount=100,
                credits=100,
                currency="XTR",
                payload="unique-wallet-payload",
            )


@pytest.mark.asyncio
async def test_mark_payment_order_paid_credits_balance_only_once(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=102, balance=3))
        await session.commit()
        repo = PaymentRepository(session)
        order = await repo.create_payment_order(
            user_id=102,
            provider="telegram_stars",
            amount=500,
            credits=50,
            currency="XTR",
            payload="stars-invoice-102",
        )

        paid_order = await repo.mark_payment_order_paid(
            order.id,
            external_payment_id="external-1",
            telegram_payment_charge_id="charge-1",
        )
        duplicate_paid_order = await repo.mark_payment_order_paid(
            order.id,
            external_payment_id="external-duplicate",
            telegram_payment_charge_id="charge-duplicate",
        )

        result = await session.execute(select(User.balance).where(User.id == 102))
        assert result.scalar_one() == 53
        assert paid_order is not None
        assert paid_order.status == PaymentOrderStatus.PAID.value
        assert paid_order.paid_at is not None
        assert paid_order.external_payment_id == "external-1"
        assert paid_order.telegram_payment_charge_id == "charge-1"
        assert duplicate_paid_order is not None
        assert duplicate_paid_order.external_payment_id == "external-1"


@pytest.mark.asyncio
async def test_complete_payment_and_credit_user_is_atomic_and_idempotent(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=105, balance=11))
        await session.commit()
        repo = PaymentRepository(session)
        order = await repo.create_payment_order(
            user_id=105,
            provider="telegram_stars_wallet_bot",
            amount=300,
            credits=300,
            currency="XTR",
            payload="wallet:105:300:unique",
        )

        completion = await repo.complete_payment_and_credit_user(
            payload="wallet:105:300:unique",
            telegram_payment_charge_id="charge-105",
            total_amount=300,
        )
        duplicate_completion = await repo.complete_payment_and_credit_user(
            payload="wallet:105:300:unique",
            telegram_payment_charge_id="duplicate-charge-105",
            total_amount=300,
        )

        balance_result = await session.execute(select(User.balance).where(User.id == 105))
        paid_order = await repo.get_payment_order_by_id(order.id)
        assert balance_result.scalar_one() == 311
        assert completion.order is not None
        assert completion.order.id == order.id
        assert completion.already_paid is False
        assert duplicate_completion.order is not None
        assert duplicate_completion.already_paid is True
        assert paid_order is not None
        assert paid_order.status == PaymentOrderStatus.PAID.value
        assert paid_order.telegram_payment_charge_id == "charge-105"
        assert paid_order.paid_at is not None


@pytest.mark.asyncio
async def test_complete_payment_and_credit_user_rejects_amount_mismatch_without_crediting(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=106, balance=21))
        await session.commit()
        repo = PaymentRepository(session)
        order = await repo.create_payment_order(
            user_id=106,
            provider="telegram_stars_wallet_bot",
            amount=500,
            credits=500,
            currency="XTR",
            payload="wallet:106:500:unique",
        )
        order_id = order.id

        with pytest.raises(ValueError):
            await repo.complete_payment_and_credit_user(
                payload="wallet:106:500:unique",
                telegram_payment_charge_id="charge-106",
                total_amount=300,
            )

        balance_result = await session.execute(select(User.balance).where(User.id == 106))
        fresh_order = await repo.get_payment_order_by_id(order_id)
        assert balance_result.scalar_one() == 21
        assert fresh_order is not None
        assert fresh_order.status == PaymentOrderStatus.CREATED.value
        assert fresh_order.telegram_payment_charge_id is None


@pytest.mark.asyncio
async def test_complete_payment_and_credit_user_returns_none_for_missing_payload(session_factory) -> None:
    async with session_factory() as session:
        repo = PaymentRepository(session)

        completion = await repo.complete_payment_and_credit_user(
            payload="missing",
            telegram_payment_charge_id="charge-missing",
            total_amount=100,
        )

        assert completion.order is None
        assert completion.already_paid is False


@pytest.mark.asyncio
async def test_mark_payment_order_failed_does_not_change_paid_order(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=103, balance=0))
        await session.commit()
        repo = PaymentRepository(session)
        order = await repo.create_payment_order(
            user_id=103,
            provider="crypto",
            amount=1000,
            credits=100,
            currency="USDT",
        )

        failed_order = await repo.mark_payment_order_failed(order.id, reason="timeout")
        paid_after_failed = await repo.mark_payment_order_paid(order.id, external_payment_id="tx-1")
        failed_after_paid = await repo.mark_payment_order_failed(order.id, reason="late failure")

        result = await session.execute(select(User.balance).where(User.id == 103))
        assert result.scalar_one() == 100
        assert failed_order is not None
        assert failed_order.metadata_ == {"failure_reason": "timeout"}
        assert paid_after_failed is not None
        assert paid_after_failed.status == PaymentOrderStatus.PAID.value
        assert failed_after_paid is not None
        assert failed_after_paid.status == PaymentOrderStatus.PAID.value
        assert failed_after_paid.metadata_ == {"failure_reason": "timeout"}


@pytest.mark.asyncio
async def test_get_user_payment_orders_returns_recent_orders(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=104, balance=0))
        await session.commit()
        repo = PaymentRepository(session)
        first = await repo.create_payment_order(104, "telegram_stars", 100, 10, "XTR")
        second = await repo.create_payment_order(104, "telegram_stars", 200, 20, "XTR")

        orders = await repo.get_user_payment_orders(104, limit=1)

        assert [order.id for order in orders] == [second.id]
        assert first.id != second.id
