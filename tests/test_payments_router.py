from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.routers import payments
from app.db.base import Base
from app.db.models import CryptoPaymentOrder, PaymentOrder, PaymentOrderStatus, User


class FakeMessage:
    def __init__(self, user_id: int = 1):
        self.chat = SimpleNamespace(id=user_id)
        self.from_user = SimpleNamespace(
            id=user_id,
            username="tester",
            first_name="Test",
            last_name=None,
            language_code="ru",
            is_bot=False,
            is_premium=False,
        )
        self.edits: list[str] = []
        self.edit_markups: list[object] = []
        self.answers: list[str] = []
        self.invoices: list[dict[str, object]] = []
        self.successful_payment = None

    async def edit_text(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.edits.append(text)
        self.edit_markups.append(reply_markup)

    async def answer(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.answers.append(text)

    async def answer_invoice(self, **kwargs) -> None:
        self.invoices.append(kwargs)


class FakeCallback:
    def __init__(self, user_id: int = 1, message: FakeMessage | None = None, data: str = ""):
        self.from_user = SimpleNamespace(
            id=user_id,
            username="tester",
            first_name="Test",
            last_name=None,
            language_code="ru",
            is_bot=False,
            is_premium=False,
        )
        self.message = message or FakeMessage(user_id)
        self.data = data
        self.answers: list[str | None] = []
        self.answer_alerts: list[bool] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text)
        self.answer_alerts.append(show_alert)


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "payments-router.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


@pytest.mark.asyncio
async def test_top_up_button_opens_stars_menu() -> None:
    message = FakeMessage(user_id=801)
    callback = FakeCallback(user_id=801, message=message, data="profile:top_up_balance")

    await payments.show_stars_top_up_menu(callback)

    assert message.edits[-1] == "Выберите способ оплаты:"
    keyboard = message.edit_markups[-1]
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [button.callback_data for button in buttons] == ["pay:method:stars", "pay:crypto", "pay:back:profile"]
    assert [button.text for button in buttons] == ["⭐ Telegram Stars", "₿ Crypto", "⬅️ Назад в профиль"]
    assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_stars_method_opens_amount_menu() -> None:
    message = FakeMessage(user_id=801)
    callback = FakeCallback(user_id=801, message=message, data="pay:method:stars")

    await payments.show_stars_amount_menu(callback)

    assert message.edits[-1] == "Выберите количество Telegram Stars:"
    keyboard = message.edit_markups[-1]
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [button.callback_data for button in buttons] == [
        "pay:stars:100",
        "pay:stars:300",
        "pay:stars:500",
        "pay:stars:1000",
        "pay:stars:3000",
        "pay:stars:5000",
        "pay:back:profile",
    ]
    assert [button.text for button in buttons] == [
        "100 ⭐",
        "300 ⭐",
        "500 ⭐",
        "1000 ⭐",
        "3000 ⭐",
        "5000 ⭐",
        "⬅️ Назад в профиль",
    ]
    assert all("Магазин" not in button.text for button in buttons)
    assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_crypto_button_shows_package_menu(monkeypatch) -> None:
    monkeypatch.setattr(payments, "is_nowpayments_configured", lambda: True)
    message = FakeMessage(user_id=811)
    callback = FakeCallback(user_id=811, message=message, data="pay:crypto")

    await payments.show_crypto_packages(callback)

    assert message.edits[-1] == "Выберите способ оплаты:"
    keyboard = message.edit_markups[-1]
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [button.callback_data for button in buttons] == [
        "pay:crypto:100",
        "pay:crypto:300",
        "pay:crypto:500",
        "pay:crypto:1000",
        "pay:crypto:3000",
        "pay:crypto:5000",
        "pay:back:profile",
    ]
    assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_crypto_button_shows_coming_soon_when_nowpayments_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(payments, "is_nowpayments_configured", lambda: False)
    callback = FakeCallback(user_id=811, data="pay:crypto")

    await payments.show_crypto_packages(callback)

    assert callback.answers[-1] == "₿ Crypto payments are not configured yet."
    assert callback.answer_alerts[-1] is True


@pytest.mark.asyncio
async def test_crypto_amount_callback_creates_nowpayments_order_without_crediting(session_factory, monkeypatch) -> None:
    class FakeNowPaymentsService:
        async def create_payment(self, *, order_id, credits, amount_usd, pay_currency=None):
            return {
                "payment_id": "np-router-1",
                "invoice_url": "https://nowpayments.test/invoice/np-router-1",
                "pay_address": "TNowPaymentsAddress",
                "pay_amount": "1.00",
                "pay_currency": "usdttrc20",
                "order_id": order_id,
            }

    monkeypatch.setattr(payments, "is_nowpayments_configured", lambda: True)
    original_provider = payments.NOWPaymentsProvider
    monkeypatch.setattr(
        payments,
        "NOWPaymentsProvider",
        lambda session: original_provider(session, service=FakeNowPaymentsService()),
    )
    async with session_factory() as session:
        session.add(User(id=812, balance=0))
        await session.commit()
        message = FakeMessage(user_id=812)
        callback = FakeCallback(user_id=812, message=message, data="pay:crypto:100")

        await payments.choose_crypto_amount(callback, session)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 812))
        order = result.scalars().one()
        crypto_order = (
            await session.execute(select(CryptoPaymentOrder).where(CryptoPaymentOrder.payment_order_id == order.id))
        ).scalar_one()
        balance = (await session.execute(select(User.balance).where(User.id == 812))).scalar_one()

        assert order.provider == "crypto"
        assert order.status == PaymentOrderStatus.PENDING.value
        assert order.amount == 100
        assert order.credits == 100
        assert order.nowpayments_payment_id == "np-router-1"
        assert crypto_order.nowpayments_payment_id == "np-router-1"
        assert crypto_order.asset == "USDT"
        assert crypto_order.network == "TRC20"
        assert crypto_order.wallet_address == "TNowPaymentsAddress"
        assert crypto_order.expected_amount == "1.00"
        assert balance == 0
        assert "Asset: USDT" in message.edits[-1]
        assert "Network: TRC20" in message.edits[-1]
        assert "Amount: 1.00" in message.edits[-1]
        assert "Address: TNowPaymentsAddress" in message.edits[-1]
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "Open Payment Page"
        assert keyboard.inline_keyboard[0][0].url == "https://nowpayments.test/invoice/np-router-1"
        assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_back_to_profile_restores_profile_screen(session_factory) -> None:
    async with session_factory() as session:
        message = FakeMessage(user_id=802)
        callback = FakeCallback(user_id=802, message=message, data="pay:back:profile")

        await payments.back_to_profile(callback, session)

        assert message.edits[-1].startswith("👤 <b>Профиль</b>")
        assert "История" not in message.edits[-1]
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "💳 Пополнить баланс"
        assert keyboard.inline_keyboard[1][0].text == "📎 Переключить способ отправки"
        assert keyboard.inline_keyboard[2][0].text == "⬅️ Назад"
        assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_stars_amount_callback_creates_order_and_sends_wallet_url(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "telegram_stars_wallet_bot_username", "@wallet_bot")
    async with session_factory() as session:
        session.add(User(id=814, balance=0))
        await session.commit()
        message = FakeMessage(user_id=814)
        callback = FakeCallback(user_id=814, message=message, data="pay:stars:300")

        await payments.choose_stars_amount(callback, session)

        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 814))).scalar_one()
        assert message.invoices == []
        assert message.edits == ["Оплата готова. Выберите способ оплаты:"]
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "Перейти к оплате ⭐"
        assert keyboard.inline_keyboard[0][0].url == f"https://t.me/wallet_bot?start=stars%3A{order.id}%3A{order.payload.rsplit(':', 1)[1]}"
        assert keyboard.inline_keyboard[1][0].text == "⬅️ Назад в профиль"
        assert order.status == PaymentOrderStatus.CREATED.value
        assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_stars_amount_requires_wallet_bot_username(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "telegram_stars_wallet_bot_username", "")
    async with session_factory() as session:
        session.add(User(id=803, balance=0))
        await session.commit()
        message = FakeMessage(user_id=803)
        callback = FakeCallback(user_id=803, message=message, data="pay:stars:500")

        await payments.choose_stars_amount(callback, session)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 803))
        assert result.scalars().all() == []
        assert message.invoices == []
        assert callback.answers[-1] == "Не удалось создать счёт на оплату. Попробуйте позже."
        assert callback.answer_alerts[-1] is True


def test_wallet_deep_link_helpers_normalize_usernames(monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "telegram_stars_return_bot_username", "@our_bot")

    assert payments.build_wallet_payment_url("@wallet_bot", 100) == "https://t.me/wallet_bot?start=100credits"
    assert payments.build_wallet_payment_url_for_payload("@wallet_bot", "stars:order:token") == "https://t.me/wallet_bot?start=stars%3Aorder%3Atoken"
    assert payments.build_wallet_return_url("stars:order:token") == "https://t.me/our_bot?start=paid_stars%3Aorder%3Atoken"


@pytest.mark.asyncio
async def test_stars_amount_callback_rejects_invalid_amount(session_factory) -> None:
    async with session_factory() as session:
        callback = FakeCallback(user_id=804, data="pay:stars:250")

        await payments.choose_stars_amount(callback, session)

        assert callback.answers[-1] == "Недоступное количество Telegram Stars."
        assert callback.answer_alerts[-1] is True
