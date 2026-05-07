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
from app.db.models import PaymentOrder, PaymentOrderStatus, User


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
    assert [button.text for button in buttons] == [
        "100 ⭐",
        "300 ⭐",
        "500 ⭐",
        "1000 ⭐",
        "3000 ⭐",
        "5000 ⭐",
        "₿ Crypto",
        "⬅️ Назад в профиль",
    ]
    assert all("Магазин" not in button.text for button in buttons)
    assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_crypto_button_shows_coming_soon_alert() -> None:
    message = FakeMessage(user_id=811)
    callback = FakeCallback(user_id=811, message=message, data="pay:crypto")

    await payments.show_crypto_payments_soon(callback)

    assert message.edits == []
    assert callback.answers[-1] == "Crypto payments скоро появятся."
    assert callback.answer_alerts[-1] is True


@pytest.mark.asyncio
async def test_crypto_amount_callback_is_placeholder_and_does_not_create_order(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=812, balance=0))
        await session.commit()
        message = FakeMessage(user_id=812)
        callback = FakeCallback(user_id=812, message=message, data="pay:crypto:100")

        await payments.reject_crypto_payment_callbacks(callback)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 812))
        assert result.scalars().all() == []
        assert message.edits == []
        assert callback.answers[-1] == "Crypto payments скоро появятся."
        assert callback.answer_alerts[-1] is True


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
async def test_stars_amount_callback_creates_order_and_sends_invoice(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "telegram_stars_wallet_bot_username", "")
    async with session_factory() as session:
        session.add(User(id=803, balance=0))
        await session.commit()
        message = FakeMessage(user_id=803)
        callback = FakeCallback(user_id=803, message=message, data="pay:stars:500")

        await payments.choose_stars_amount(callback, session)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 803))
        order = result.scalars().one()
        assert order.provider == "telegram_stars"
        assert order.amount == 500
        assert order.credits == 500
        assert order.currency == "XTR"
        assert order.payload is not None
        assert order.payload.startswith(f"stars:{order.id}:")
        assert message.edits == []
        assert len(message.invoices) == 1
        invoice = message.invoices[0]
        assert invoice["title"] == "Кредиты"
        assert invoice["description"] == "500 кредитов для IMai"
        assert invoice["payload"] == order.payload
        assert invoice["currency"] == "XTR"
        assert invoice["provider_token"] == ""
        assert len(invoice["prices"]) == 1
        assert invoice["prices"][0].label == "500 кредитов"
        assert invoice["prices"][0].amount == 500
        assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_stars_amount_callback_shows_wallet_and_invoice_fallback(session_factory, monkeypatch) -> None:
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
        assert keyboard.inline_keyboard[0][0].text == "Перейти к оплате"
        assert keyboard.inline_keyboard[0][0].url == f"https://t.me/wallet_bot?start=stars%3A{order.id}%3A{order.payload.rsplit(':', 1)[1]}"
        assert keyboard.inline_keyboard[1][0].text == "⭐ Оплатить здесь"
        assert keyboard.inline_keyboard[1][0].callback_data == f"pay:invoice:{order.id}"
        assert keyboard.inline_keyboard[2][0].text == "⬅️ Назад в профиль"
        assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_invoice_fallback_button_sends_invoice_for_existing_order(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "telegram_stars_wallet_bot_username", "@wallet_bot")
    async with session_factory() as session:
        session.add(User(id=815, balance=0))
        await session.commit()
        message = FakeMessage(user_id=815)
        callback = FakeCallback(user_id=815, message=message, data="pay:stars:100")
        await payments.choose_stars_amount(callback, session)
        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 815))).scalar_one()
        fallback_callback = FakeCallback(user_id=815, message=message, data=f"pay:invoice:{order.id}")

        await payments.pay_stars_invoice_here(fallback_callback, session)

        assert len(message.invoices) == 1
        invoice = message.invoices[0]
        assert invoice["payload"] == order.payload
        assert invoice["currency"] == "XTR"
        assert invoice["provider_token"] == ""
        assert len(invoice["prices"]) == 1
        assert invoice["prices"][0].amount == 100
        assert fallback_callback.answers[-1] is None


@pytest.mark.asyncio
async def test_pre_checkout_accepts_existing_matching_order(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=809, balance=0))
        await session.commit()
        service = payments.PaymentService(session)
        order = await service.create_stars_order(user_id=809, stars_amount=300)
        pre_checkout = FakePreCheckoutQuery(user_id=809, invoice_payload=order.payload, total_amount=300)

        await payments.process_stars_pre_checkout(pre_checkout, session)

        assert pre_checkout.answers == [{"ok": True, "error_message": None}]


@pytest.mark.asyncio
async def test_pre_checkout_rejects_missing_or_mismatched_order(session_factory) -> None:
    async with session_factory() as session:
        pre_checkout = FakePreCheckoutQuery(user_id=810, invoice_payload="missing", total_amount=100)

        await payments.process_stars_pre_checkout(pre_checkout, session)

        assert pre_checkout.answers == [{"ok": False, "error_message": "Платёжный заказ не найден"}]


@pytest.mark.asyncio
async def test_pre_checkout_rejects_existing_payload_with_mismatched_amount(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=816, balance=0))
        await session.commit()
        service = payments.PaymentService(session)
        order = await service.create_stars_order(user_id=816, stars_amount=300)
        pre_checkout = FakePreCheckoutQuery(user_id=816, invoice_payload=order.payload, total_amount=100)

        await payments.process_stars_pre_checkout(pre_checkout, session)

        fresh_order = (await session.execute(select(PaymentOrder).where(PaymentOrder.id == order.id))).scalar_one()
        fresh_balance = (await session.execute(select(User.balance).where(User.id == 816))).scalar_one()
        assert pre_checkout.answers == [{"ok": False, "error_message": "Платёжный заказ не найден"}]
        assert fresh_order.status == PaymentOrderStatus.CREATED.value
        assert fresh_balance == 0


@pytest.mark.asyncio
async def test_successful_payment_completes_order_and_credits_once(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=813, balance=9))
        await session.commit()
        service = payments.PaymentService(session)
        order = await service.create_stars_order(user_id=813, stars_amount=100)
        message = FakeMessage(user_id=813)
        message.successful_payment = SimpleNamespace(
            invoice_payload=order.payload,
            total_amount=100,
            telegram_payment_charge_id="charge-813",
        )

        await payments.process_successful_stars_payment(message, session)
        await payments.process_successful_stars_payment(message, session)

        result = await session.execute(select(User.balance).where(User.id == 813))
        fresh_order = (await session.execute(select(PaymentOrder).where(PaymentOrder.id == order.id))).scalar_one()
        assert result.scalar_one() == 109
        assert fresh_order.status == PaymentOrderStatus.PAID.value
        assert fresh_order.telegram_payment_charge_id == "charge-813"
        assert message.answers == [
            "✅ Платёж получен. Добавлено кредитов: 100. Баланс: 109",
            "✅ Платёж получен. Добавлено кредитов: 100. Баланс: 109",
        ]


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
