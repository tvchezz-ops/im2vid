from __future__ import annotations

import os
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("WALLET_BOT_TOKEN", "wallet-test-token")
os.environ.setdefault("MAIN_BOT_USERNAME", "main_bot")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./wallet-test.db")
os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from wallet_bot.main import (  # noqa: E402
    WALLET_PAYMENT_PROVIDER,
    WalletSettings,
    extract_start_payload,
    parse_amount_from_invoice_payload,
    parse_amount_from_start_payload,
    process_pre_checkout_query,
    process_successful_payment,
    start_command,
)
from app.bot.routers.payments import build_wallet_payment_url  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import PaymentOrder, PaymentOrderStatus, User  # noqa: E402


class FakeMessage:
    def __init__(self, user_id: int = 1, text: str | None = None):
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.answers: list[dict[str, object]] = []
        self.invoices: list[dict[str, object]] = []
        self.successful_payment = None

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append({"text": text, "reply_markup": reply_markup})

    async def answer_invoice(self, **kwargs) -> None:
        self.invoices.append(kwargs)


class FakePreCheckoutQuery:
    def __init__(self, user_id: int, invoice_payload: str, total_amount: int, currency: str = "XTR"):
        self.from_user = SimpleNamespace(id=user_id)
        self.invoice_payload = invoice_payload
        self.total_amount = total_amount
        self.currency = currency
        self.answers: list[dict[str, object]] = []

    async def answer(self, ok: bool, error_message: str | None = None) -> None:
        self.answers.append({"ok": ok, "error_message": error_message})


@pytest.fixture
def settings() -> WalletSettings:
    return WalletSettings(
        WALLET_BOT_TOKEN="wallet-token",
        MAIN_BOT_USERNAME="@main_bot",
        DATABASE_URL="sqlite+aiosqlite:///./wallet-test.db",
        WALLET_ALLOWED_AMOUNTS="100,300,500,1000,3000,5000",
    )


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "wallet-bot.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


def test_parse_amount_from_start_payload_accepts_supported_formats() -> None:
    assert parse_amount_from_start_payload("100credits") == 100
    assert parse_amount_from_start_payload("pay_100") == 100
    assert parse_amount_from_start_payload("100") is None
    assert parse_amount_from_start_payload("pay_100_extra") is None


def test_extract_start_payload_reads_message_text_before_command_args() -> None:
    message = FakeMessage(text="/start 100credits")

    assert extract_start_payload(message, SimpleNamespace(args="pay_300")) == "100credits"
    assert extract_start_payload(FakeMessage(text="/start"), SimpleNamespace(args="pay_300")) == ""
    assert extract_start_payload(FakeMessage(text=None), SimpleNamespace(args="pay_300")) == "pay_300"


def test_settings_parse_comma_separated_allowed_amounts(settings) -> None:
    assert settings.allowed_amounts == (100, 300, 500, 1000, 3000, 5000)


@pytest.mark.asyncio
async def test_start_without_payload_sends_nothing_and_logs_ignored_start(settings, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wallet_bot.main")
    message = FakeMessage(user_id=701)

    await start_command(message, SimpleNamespace(args=None), settings, None)

    assert message.answers == []
    assert message.invoices == []
    assert any(isinstance(record.msg, dict) and record.msg.get("action") == "ignored_start" for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["100credits", "pay_100"])
async def test_start_with_valid_deep_link_creates_order_and_sends_stars_invoice(
    settings,
    session_factory,
    payload: str,
) -> None:
    async with session_factory() as session:
        session.add(User(id=702, balance=0))
        await session.commit()
    message = FakeMessage(user_id=702, text=f"/start {payload}")

    await start_command(message, SimpleNamespace(args=None), settings, session_factory)

    assert message.answers == []
    assert len(message.invoices) == 1
    invoice = message.invoices[0]
    assert invoice["title"] == "100 credits"
    assert invoice["description"] == "Top up 100 credits"
    assert invoice["provider_token"] == ""
    assert invoice["currency"] == "XTR"
    assert parse_amount_from_invoice_payload(invoice["payload"]) == 100
    assert invoice["prices"][0].label == "100 credits"
    assert invoice["prices"][0].amount == 100
    async with session_factory() as session:
        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 702))
        order = result.scalar_one()
    assert invoice["payload"] == order.payload
    assert order.provider == WALLET_PAYMENT_PROVIDER
    assert order.status == PaymentOrderStatus.CREATED.value
    assert order.amount == 100
    assert order.credits == 100
    assert order.currency == "XTR"
    assert order.payload is not None
    assert order.payload.startswith("wallet:702:100:")


@pytest.mark.asyncio
async def test_start_with_invalid_amount_sends_invalid_payment_link_and_creates_no_order(settings, session_factory) -> None:
    message = FakeMessage(user_id=703, text="/start pay_250")

    await start_command(message, SimpleNamespace(args=None), settings, session_factory)

    assert message.invoices == []
    assert message.answers == [{"text": "Invalid payment link.", "reply_markup": None}]
    async with session_factory() as session:
        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 703))
        assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_start_with_malformed_payload_sends_invalid_payment_link(settings) -> None:
    message = FakeMessage(user_id=706, text="/start 100")

    await start_command(message, SimpleNamespace(args=None), settings, None)

    assert message.invoices == []
    assert message.answers == [{"text": "Invalid payment link.", "reply_markup": None}]


def test_main_bot_wallet_deep_link_uses_start_parameter() -> None:
    assert build_wallet_payment_url("@wallet_bot", 100) == "https://t.me/wallet_bot?start=100credits"
    assert "?=100credits" not in build_wallet_payment_url("@wallet_bot", 100)


@pytest.mark.asyncio
async def test_pre_checkout_query_ok_only_for_existing_unpaid_order_with_matching_amount_and_currency(
    settings,
    session_factory,
) -> None:
    async with session_factory() as session:
        session.add(User(id=704, balance=0))
        await session.commit()
    message = FakeMessage(user_id=704)
    await start_command(message, SimpleNamespace(args="300credits"), settings, session_factory)
    invoice_payload = message.invoices[0]["payload"]

    accepted_query = FakePreCheckoutQuery(user_id=704, invoice_payload=invoice_payload, total_amount=300)
    missing_query = FakePreCheckoutQuery(user_id=704, invoice_payload="missing", total_amount=300)
    mismatch_query = FakePreCheckoutQuery(user_id=704, invoice_payload=invoice_payload, total_amount=500)
    wrong_currency_query = FakePreCheckoutQuery(user_id=704, invoice_payload=invoice_payload, total_amount=300, currency="USD")

    await process_pre_checkout_query(accepted_query, settings, session_factory)
    await process_pre_checkout_query(missing_query, settings, session_factory)
    await process_pre_checkout_query(mismatch_query, settings, session_factory)
    await process_pre_checkout_query(wrong_currency_query, settings, session_factory)

    assert accepted_query.answers == [{"ok": True, "error_message": None}]
    assert missing_query.answers == [{"ok": False, "error_message": "Payment order not found"}]
    assert mismatch_query.answers == [{"ok": False, "error_message": "Payment order not found"}]
    assert wrong_currency_query.answers == [{"ok": False, "error_message": "Payment order not found"}]


@pytest.mark.asyncio
async def test_successful_payment_marks_order_paid_and_credits_balance_once(settings, session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=705, balance=7))
        await session.commit()
    invoice_message = FakeMessage(user_id=705)
    await start_command(invoice_message, SimpleNamespace(args="100credits"), settings, session_factory)
    invoice_payload = invoice_message.invoices[0]["payload"]
    message = FakeMessage(user_id=705)
    message.successful_payment = SimpleNamespace(
        invoice_payload=invoice_payload,
        total_amount=100,
        currency="XTR",
        telegram_payment_charge_id="charge-secret",
    )

    await process_successful_payment(message, settings, session_factory)
    await process_successful_payment(message, settings, session_factory)

    assert message.invoices == []
    assert [answer["text"] for answer in message.answers] == [
        "✅ Payment received.\n100 credits added to your balance.",
        "✅ Payment received.\n100 credits added to your balance.",
    ]
    keyboard = message.answers[0]["reply_markup"]
    assert len(keyboard.inline_keyboard) == 1
    assert len(keyboard.inline_keyboard[0]) == 1
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Return to generation bot"
    assert button.url == "https://t.me/main_bot?start=payment_success"
    async with session_factory() as session:
        balance_result = await session.execute(select(User.balance).where(User.id == 705))
        order_result = await session.execute(select(PaymentOrder).where(PaymentOrder.payload == invoice_payload))
        paid_order = order_result.scalar_one()
    assert balance_result.scalar_one() == 107
    assert paid_order.status == PaymentOrderStatus.PAID.value
    assert paid_order.telegram_payment_charge_id == "charge-secret"
    assert paid_order.paid_at is not None
    paid_query = FakePreCheckoutQuery(user_id=705, invoice_payload=invoice_payload, total_amount=100)

    await process_pre_checkout_query(paid_query, settings, session_factory)

    assert paid_query.answers == [{"ok": False, "error_message": "Payment order not found"}]