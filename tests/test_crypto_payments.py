from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.db.base import Base
from app.db.models import CryptoPaymentOrder, PaymentOrder, PaymentOrderStatus, PaymentProvider, User
from app.services.crypto_payments import StubCryptoPaymentProvider
from app.services.nowpayments import NOWPaymentsProvider


class FakeNowPaymentsService:
    async def create_payment(self, *, order_id, credits, amount_usd, pay_currency=None):
        return {
            "payment_id": "np-provider-1",
            "invoice_url": "https://nowpayments.test/invoice/np-provider-1",
            "pay_address": "TProviderAddress",
            "pay_amount": "2.50",
            "pay_currency": "usdttrc20",
            "order_id": order_id,
        }

    async def get_payment_status(self, payment_id: str):
        return {
            "payment_id": payment_id,
            "payment_status": "finished",
            "payin_hash": "tx-provider-1",
            "actually_paid": "2.50",
        }


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
async def test_stub_crypto_provider_creates_draft_order_without_crediting(session_factory, monkeypatch) -> None:
    async with session_factory() as session:
        session.add(User(id=901, balance=12))
        await session.commit()
        provider = StubCryptoPaymentProvider(session)

        invoice = await provider.create_invoice(
            user_id=901,
            amount_credits=250,
            asset="usdt",
            network="trc20",
        )

        payment_order_result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 901))
        payment_order = payment_order_result.scalar_one()
        crypto_order_result = await session.execute(
            select(CryptoPaymentOrder).where(CryptoPaymentOrder.payment_order_id == payment_order.id)
        )
        crypto_order = crypto_order_result.scalar_one()
        balance_result = await session.execute(select(User.balance).where(User.id == 901))

        assert invoice.invoice_id == str(payment_order.id)
        assert invoice.asset == "USDT"
        assert invoice.network == "TRC20"
        assert invoice.amount == 250
        assert invoice.address == ""
        assert invoice.payment_url is None
        assert invoice.expires_at > datetime.now(timezone.utc)
        assert payment_order.provider == PaymentProvider.CRYPTO.value
        assert payment_order.status == PaymentOrderStatus.CREATED.value
        assert payment_order.credits == 250
        assert payment_order.currency == "USDT"
        assert crypto_order.status == "draft"
        assert crypto_order.asset == "USDT"
        assert crypto_order.network == "TRC20"
        assert crypto_order.wallet_address == ""
        assert crypto_order.expected_amount == "250"
        assert balance_result.scalar_one() == 12


@pytest.mark.asyncio
async def test_stub_crypto_provider_verify_payment_is_always_pending(session_factory) -> None:
    async with session_factory() as session:
        provider = StubCryptoPaymentProvider(session)

        status = await provider.verify_payment("invoice-id")

        assert status.status == "pending"
        assert status.tx_hash is None
        assert status.paid_amount is None


@pytest.mark.asyncio
async def test_nowpayments_provider_creates_pending_order_without_crediting(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=902, balance=17))
        await session.commit()
        provider = NOWPaymentsProvider(session, service=FakeNowPaymentsService())

        invoice = await provider.create_invoice(
            user_id=902,
            amount_credits=250,
            asset="USDT",
            network="TRC20",
        )

        payment_order = (await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 902))).scalar_one()
        crypto_order = (
            await session.execute(select(CryptoPaymentOrder).where(CryptoPaymentOrder.payment_order_id == payment_order.id))
        ).scalar_one()
        balance = (await session.execute(select(User.balance).where(User.id == 902))).scalar_one()

        assert invoice.invoice_id == "np-provider-1"
        assert invoice.asset == "USDT"
        assert invoice.network == "TRC20"
        assert invoice.amount == "2.50"
        assert invoice.address == "TProviderAddress"
        assert invoice.payment_url == "https://nowpayments.test/invoice/np-provider-1"
        assert payment_order.provider == PaymentProvider.CRYPTO.value
        assert payment_order.status == PaymentOrderStatus.PENDING.value
        assert payment_order.credits == 250
        assert payment_order.nowpayments_payment_id == "np-provider-1"
        assert crypto_order.status == "pending"
        assert crypto_order.nowpayments_payment_id == "np-provider-1"
        assert crypto_order.pay_currency == "usdttrc20"
        assert crypto_order.wallet_address == "TProviderAddress"
        assert crypto_order.expected_amount == "2.50"
        assert balance == 17


@pytest.mark.asyncio
async def test_nowpayments_provider_verify_payment_maps_provider_status(session_factory) -> None:
    async with session_factory() as session:
        provider = NOWPaymentsProvider(session, service=FakeNowPaymentsService())

        status = await provider.verify_payment("np-provider-1")

        assert status.status == "paid"
        assert status.tx_hash == "tx-provider-1"
        assert status.paid_amount == "2.50"
