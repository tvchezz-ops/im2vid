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
from app.db.models import PaymentOrder, User


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


class FakePreCheckoutQuery:
    def __init__(self, user_id: int, invoice_payload: str, total_amount: int, language_code: str = "ru"):
        self.from_user = SimpleNamespace(id=user_id, language_code=language_code)
        self.invoice_payload = invoice_payload
        self.total_amount = total_amount
        self.answers: list[dict[str, object]] = []

    async def answer(self, ok: bool, error_message: str | None = None) -> None:
        self.answers.append({"ok": ok, "error_message": error_message})


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
        "pay:crypto",
        "pay:back:profile",
    ]
    assert all("Магазин" not in button.text for button in buttons)
    assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_crypto_button_shows_package_menu() -> None:
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
async def test_crypto_amount_callback_creates_nowpayments_order(session_factory, monkeypatch) -> None:
    class FakeNowPaymentsService:
        async def create_payment(self, *, order_id, credits, amount_usd, pay_currency=None):
            return {
                "payment_id": "np-router-1",
                "payment_url": "https://nowpayments.test/pay/np-router-1",
                "pay_address": "wallet-address",
                "pay_currency": "usdttrc20",
                "price_amount": amount_usd,
                "price_currency": "usd",
            }

    monkeypatch.setattr(payments, "is_nowpayments_configured", lambda: True)
    monkeypatch.setattr(payments, "NowPaymentsService", FakeNowPaymentsService)
    async with session_factory() as session:
        session.add(User(id=812, balance=0))
        await session.commit()
        message = FakeMessage(user_id=812)
        callback = FakeCallback(user_id=812, message=message, data="pay:crypto:100")

        await payments.choose_crypto_amount(callback, session)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 812))
        order = result.scalars().one()
        assert order.provider == "crypto"
        assert order.amount == 100
        assert order.credits == 100
        assert order.currency == "USD"
        assert order.nowpayments_payment_id == "np-router-1"
        assert message.edits[-1] == "Оплата готова. Выберите способ оплаты:"
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "Перейти к оплате"
        assert keyboard.inline_keyboard[0][0].url == "https://nowpayments.test/pay/np-router-1"
        assert keyboard.inline_keyboard[1][0].text == "Проверяем оплату..."
        assert keyboard.inline_keyboard[1][0].callback_data == f"pay:crypto:check:{order.id}"
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
async def test_stars_amount_callback_opens_wallet_bot_without_creating_order_or_invoice(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "wallet_bot_username", "@wallet_bot")
    monkeypatch.setattr(payments.settings, "telegram_stars_wallet_bot_username", "")
    async with session_factory() as session:
        session.add(User(id=803, balance=0))
        await session.commit()
        message = FakeMessage(user_id=803)
        callback = FakeCallback(user_id=803, message=message, data="pay:stars:500")

        await payments.choose_stars_amount(callback)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 803))
        assert result.scalars().all() == []
        assert message.invoices == []
        assert message.edits == [
            "To pay with Telegram Stars, open the wallet bot.\nAfter payment, return here and your credits will already be added."
        ]
        keyboard = message.edit_markups[-1]
        assert len(keyboard.inline_keyboard) == 1
        assert keyboard.inline_keyboard[0][0].text == "Pay 500 ⭐"
        assert keyboard.inline_keyboard[0][0].url == "https://t.me/wallet_bot?start=500credits"
        assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_stars_amount_callback_uses_legacy_wallet_username_fallback(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "wallet_bot_username", "")
    monkeypatch.setattr(payments.settings, "telegram_stars_wallet_bot_username", "@legacy_wallet_bot")
    async with session_factory() as session:
        session.add(User(id=809, balance=0))
        await session.commit()
        message = FakeMessage(user_id=809)
        callback = FakeCallback(user_id=809, message=message, data="pay:stars:300")

        await payments.choose_stars_amount(callback)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 809))
        assert result.scalars().all() == []
        assert message.invoices == []
        assert message.edits == [
            "To pay with Telegram Stars, open the wallet bot.\nAfter payment, return here and your credits will already be added."
        ]
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "Pay 300 ⭐"
        assert keyboard.inline_keyboard[0][0].url == "https://t.me/legacy_wallet_bot?start=300credits"
        assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_stars_amount_callback_requires_wallet_bot_username(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "wallet_bot_username", "")
    monkeypatch.setattr(payments.settings, "telegram_stars_wallet_bot_username", "")
    async with session_factory() as session:
        callback = FakeCallback(user_id=810, data="pay:stars:100")

        await payments.choose_stars_amount(callback)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 810))
        assert result.scalars().all() == []
        assert callback.answers[-1] == "Не удалось создать счёт на оплату. Попробуйте позже."
        assert callback.answer_alerts[-1] is True


def test_wallet_deep_link_helpers_normalize_usernames(monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "telegram_stars_return_bot_username", "@our_bot")

    assert payments.build_wallet_payment_url("@wallet_bot", 100) == "https://t.me/wallet_bot?start=100credits"
    assert payments.build_wallet_return_url("stars_token") == "https://t.me/our_bot?start=paid_stars_token"


@pytest.mark.asyncio
async def test_stars_amount_callback_rejects_invalid_amount(session_factory) -> None:
    async with session_factory() as session:
        callback = FakeCallback(user_id=804, data="pay:stars:250")

        await payments.choose_stars_amount(callback)

        assert callback.answers[-1] == "Недоступное количество Telegram Stars."
        assert callback.answer_alerts[-1] is True
