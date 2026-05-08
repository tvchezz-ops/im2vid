from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.routers import payments
from app.db.base import Base
from app.db.models import PaymentOrder, PaymentOrderStatus, User
from app.db.repositories import PaymentRepository


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


class FakeNoModifiedMessage(FakeMessage):
    async def edit_text(self, text: str, reply_markup=None, parse_mode=None) -> None:
        if self.edits and self.edits[-1] == text:
            raise TelegramBadRequest(method=None, message="Bad Request: message is not modified")
        await super().edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


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


class FakeState:
    def __init__(self):
        self.data: dict[str, object] = {}

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)


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
    state = FakeState()

    await payments.show_stars_top_up_menu(callback, state)

    assert message.edits[-1] == "Выберите способ оплаты:"
    keyboard = message.edit_markups[-1]
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [button.callback_data for button in buttons] == ["pay:method:stars", "pay:crypto", "pay:back:profile"]
    assert [button.text for button in buttons] == ["⭐ Telegram Stars", "₿ Crypto", "⬅️ Назад в профиль"]
    assert (await state.get_data())["payment_screen"] == "methods"
    assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_stars_method_opens_amount_menu() -> None:
    message = FakeMessage(user_id=801)
    callback = FakeCallback(user_id=801, message=message, data="pay:method:stars")
    state = FakeState()

    await payments.show_stars_amount_menu(callback, state)

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
        "pay:back:methods",
    ]
    assert [button.text for button in buttons] == [
        "100 ⭐",
        "300 ⭐",
        "500 ⭐",
        "1000 ⭐",
        "3000 ⭐",
        "5000 ⭐",
        "⬅️ Назад",
    ]
    assert all("Магазин" not in button.text for button in buttons)
    assert (await state.get_data())["payment_screen"] == "stars_amounts"
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
        "pay:back:methods",
    ]
    assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_profile_topup_back_returns_profile(session_factory) -> None:
    async with session_factory() as session:
        message = FakeMessage(user_id=821)
        state = FakeState()

        await payments.show_stars_top_up_menu(
            FakeCallback(user_id=821, message=message, data="profile:top_up_balance"),
            state,
        )
        await payments.back_to_profile(
            FakeCallback(user_id=821, message=message, data="pay:back:profile"),
            session,
            state,
        )

        assert message.edits[-1].startswith("👤 <b>Профиль</b>")
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].callback_data == "profile:top_up_balance"
        assert (await state.get_data())["payment_screen"] == "profile"


@pytest.mark.asyncio
async def test_methods_stars_back_returns_methods() -> None:
    message = FakeMessage(user_id=822)
    state = FakeState()

    await payments.show_stars_top_up_menu(
        FakeCallback(user_id=822, message=message, data="profile:top_up_balance"),
        state,
    )
    await payments.show_stars_amount_menu(
        FakeCallback(user_id=822, message=message, data="pay:method:stars"),
        state,
    )
    callback = FakeCallback(user_id=822, message=message, data="pay:back:methods")
    await payments.back_to_payment_methods(callback, state)

    assert message.edits[-1] == "Выберите способ оплаты:"
    keyboard = message.edit_markups[-1]
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [button.callback_data for button in buttons] == ["pay:method:stars", "pay:crypto", "pay:back:profile"]
    assert (await state.get_data())["payment_screen"] == "methods"
    assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_repeated_back_ignores_message_is_not_modified() -> None:
    message = FakeNoModifiedMessage(user_id=823)
    state = FakeState()
    callback = FakeCallback(user_id=823, message=message, data="pay:back:methods")

    await payments.back_to_payment_methods(callback, state)
    await payments.back_to_payment_methods(callback, state)

    assert message.edits == ["Выберите способ оплаты:"]
    assert callback.answers == [None, None]
    assert (await state.get_data())["payment_screen"] == "methods"


@pytest.mark.asyncio
async def test_crypto_button_shows_coming_soon_when_nowpayments_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(payments, "is_nowpayments_configured", lambda: False)
    callback = FakeCallback(user_id=811, data="pay:crypto")

    await payments.show_crypto_packages(callback)

    assert callback.answers[-1] == "₿ Crypto payments are not configured yet."
    assert callback.answer_alerts[-1] is True


@pytest.mark.asyncio
async def test_crypto_button_does_not_crash_when_api_key_empty(monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "nowpayments_api_key", "")
    monkeypatch.setattr(payments.settings, "nowpayments_ipn_secret", "ipn-secret")
    monkeypatch.setattr(payments.settings, "nowpayments_base_url", "https://api.nowpayments.io")
    callback = FakeCallback(user_id=811, data="pay:crypto")

    await payments.show_crypto_packages(callback)

    assert callback.answers[-1] == "₿ Crypto payments are not configured yet."
    assert callback.answer_alerts[-1] is True


@pytest.mark.asyncio
async def test_crypto_amount_callback_creates_nowpayments_order_without_crediting(session_factory, monkeypatch) -> None:
    class FakeNowPaymentsService:
        def __init__(self, *, session):
            self.session = session

        async def create_payment_order_link(self, user_id: int, credits: int):
            repo = PaymentRepository(self.session)
            order = await repo.create_payment_order(
                user_id=user_id,
                provider="nowpayments",
                amount=credits,
                credits=credits,
                currency="USD",
                metadata={"payment_url": "https://nowpayments.test/invoice/np-router-1", "price_amount": "1.30"},
            )
            order = await repo.update_nowpayments_order_metadata(
                order.id,
                payment_id="np-router-1",
                status=PaymentOrderStatus.PENDING.value,
                metadata={"payment_url": "https://nowpayments.test/invoice/np-router-1", "price_amount": "1.30"},
            )
            return SimpleNamespace(
                order=order,
                payment_url="https://nowpayments.test/invoice/np-router-1",
                price_amount="1.30",
                payment_id="np-router-1",
            )

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
        balance = (await session.execute(select(User.balance).where(User.id == 812))).scalar_one()

        assert order.provider == "nowpayments"
        assert order.status == PaymentOrderStatus.PENDING.value
        assert order.amount == 100
        assert order.credits == 100
        assert order.nowpayments_payment_id == "np-router-1"
        assert order.metadata_["payment_url"] == "https://nowpayments.test/invoice/np-router-1"
        assert balance == 0
        assert message.edits[-1] == (
            "₿ Crypto payment\n\n"
            "Credits: 100\n"
            "Amount: $1.30\n\n"
            "Payment is processed securely by NOWPayments.\n"
            "Click the button below to pay."
        )
        assert "Address" not in message.edits[-1]
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "Оплатить через NOWPayments"
        assert keyboard.inline_keyboard[0][0].url == "https://nowpayments.test/invoice/np-router-1"
        assert keyboard.inline_keyboard[1][0].callback_data == "pay:back:crypto_amounts"
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
    monkeypatch.setattr(payments.settings, "wallet_bot_username", "@wallet_bot")
    async with session_factory() as session:
        session.add(User(id=814, balance=0))
        await session.commit()
        message = FakeMessage(user_id=814)
        callback = FakeCallback(user_id=814, message=message, data="pay:stars:300")

        await payments.choose_stars_amount(callback, session)

        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 814))).scalar_one()
        assert message.invoices == []
        assert message.edits == ["Вы выбрали: 300 ⭐\nНажмите кнопку ниже, чтобы перейти к оплате."]
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "Перейти к оплате ⭐"
        wallet_url = keyboard.inline_keyboard[0][0].url
        parsed_url = urlparse(wallet_url)
        payload_from_url = parse_qs(parsed_url.query)["start"][0]
        assert wallet_url.startswith("https://t.me/")
        assert wallet_url == f"https://t.me/wallet_bot?start={order.payload}"
        assert order.payload == payload_from_url
        assert keyboard.inline_keyboard[1][0].text == "⬅️ Назад"
        assert keyboard.inline_keyboard[1][0].callback_data == "pay:back:stars_amounts"
        assert order.provider == "telegram_stars"
        assert order.amount == 300
        assert order.credits == 300
        assert order.currency == "XTR"
        assert order.status == PaymentOrderStatus.CREATED.value
        assert order.payload is not None
        assert len(order.payload) <= 64
        assert payments.TELEGRAM_DEEP_LINK_PAYLOAD_RE.fullmatch(order.payload) is not None
        assert callback.answers[-1] is None


@pytest.mark.asyncio
async def test_stars_amount_logs_redirect_without_payload_secret(session_factory, monkeypatch, caplog) -> None:
    monkeypatch.setattr(payments.settings, "wallet_bot_username", "@wallet_bot")
    caplog.set_level("INFO")
    async with session_factory() as session:
        session.add(User(id=815, balance=0))
        await session.commit()
        message = FakeMessage(user_id=815)
        callback = FakeCallback(user_id=815, message=message, data="pay:stars:100")

        await payments.choose_stars_amount(callback, session)

        order = (await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 815))).scalar_one()
        redirect_logs = [record.msg for record in caplog.records if isinstance(record.msg, dict) and record.msg.get("action") == "stars_wallet_redirect_created"]
        assert redirect_logs == [
            {
                "action": "stars_wallet_redirect_created",
                "wallet_bot": "wallet_bot",
                "payload_length": len(order.payload),
                "order_id": str(order.id),
            }
        ]
        assert order.payload not in str(redirect_logs[0])


@pytest.mark.asyncio
async def test_stars_amount_requires_wallet_bot_username(session_factory, monkeypatch, caplog) -> None:
    monkeypatch.setattr(payments.settings, "wallet_bot_username", None)
    caplog.set_level("INFO")
    async with session_factory() as session:
        session.add(User(id=803, balance=0))
        await session.commit()
        message = FakeMessage(user_id=803)
        callback = FakeCallback(user_id=803, message=message, data="pay:stars:500")

        await payments.choose_stars_amount(callback, session)

        result = await session.execute(select(PaymentOrder).where(PaymentOrder.user_id == 803))
        assert result.scalars().all() == []
        assert message.invoices == []
        assert message.edits == []
        assert callback.answers[-1] == "Оплата через Telegram Stars временно недоступна."
        assert callback.answer_alerts[-1] is True
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "stars_wallet_not_configured"
            and "order_id" not in record.msg
            for record in caplog.records
        )


def test_wallet_deep_link_helpers_normalize_usernames(monkeypatch) -> None:
    monkeypatch.setattr(payments.settings, "telegram_stars_return_bot_username", "@our_bot")

    assert payments.build_wallet_payment_url("@wallet_bot", 100) == "https://t.me/wallet_bot?start=100credits"
    assert payments.build_wallet_payment_url_for_payload("@wallet_bot", "stars_order_token") == "https://t.me/wallet_bot?start=stars_order_token"
    assert payments.build_wallet_return_url("stars:order:token") == "https://t.me/our_bot?start=paid_stars%3Aorder%3Atoken"


def test_wallet_deep_link_rejects_unsafe_payload() -> None:
    with pytest.raises(ValueError):
        payments.build_wallet_payment_url_for_payload("wallet_bot", "stars:order:token")
    with pytest.raises(ValueError):
        payments.build_wallet_payment_url_for_payload("wallet_bot", "s" * 65)


@pytest.mark.asyncio
async def test_stars_amount_callback_rejects_invalid_amount(session_factory) -> None:
    async with session_factory() as session:
        callback = FakeCallback(user_id=804, data="pay:stars:250")

        await payments.choose_stars_amount(callback, session)

        assert callback.answers[-1] == "Недоступное количество Telegram Stars."
        assert callback.answer_alerts[-1] is True
