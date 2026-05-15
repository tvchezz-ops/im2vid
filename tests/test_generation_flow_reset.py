from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.routers import generations, profile, start
from app.bot.keyboards import build_main_menu_keyboard, get_button_text
from app.bot.states import GenerationStates
from app.config import settings
from app.db.base import Base
from app.db.models import PaymentOrderStatus, ReferralEvent, User
from app.i18n import t
from app.services.payments import PaymentService


class FakeState:
    def __init__(self, data: dict[str, object] | None = None):
        self.data = data or {}
        self.state = None

    async def get_state(self):
        return self.state

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def set_state(self, state) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


class FakeMessage:
    def __init__(self, user_id: int = 1, text: str | None = None, language_code: str | None = "ru"):
        self.chat = SimpleNamespace(id=user_id)
        self.bot = object()
        self.from_user = SimpleNamespace(
            id=user_id,
            username="tester",
            first_name="Test",
            last_name=None,
            language_code=language_code,
            is_bot=False,
            is_premium=False,
        )
        self.text = text
        self.answers: list[str] = []
        self.answer_markups: list[object] = []

    async def answer(self, text: str, reply_markup=None, parse_mode=None, **kwargs) -> None:
        self.answers.append(text)
        self.answer_markups.append(reply_markup)


def _assert_main_menu_keyboard(markup, lang: str = "ru") -> None:
    assert markup is not None
    assert markup.keyboard[0][0].text == get_button_text("main.generations", lang)
    assert markup.keyboard[0][1].text == get_button_text("main.profile", lang)
    assert len(markup.keyboard) == 1
    assert all("Магазин" not in button.text and "Shop" not in button.text for row in markup.keyboard for button in row)


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "generation-flow-reset.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


@pytest.mark.asyncio
async def test_reset_generation_flow_deletes_temp_files_and_clears_state(tmp_path, caplog) -> None:
    temp_file = tmp_path / "input.png"
    temp_file.write_bytes(b"input")
    state = FakeState(
        {
            "input_media": {"type": "image", "count": 1},
            "input_media_items": [{"local_path": str(temp_file)}],
            "input_image_file_id": "file-id",
            "prompt": "hello",
            "last_user_id": 999,
        }
    )
    state.state = GenerationStates.waiting_for_prompt

    with caplog.at_level(logging.INFO):
        await generations.reset_generation_flow(state, reason="test_reset")

    assert not temp_file.exists()
    assert state.state is None
    assert state.data == {}
    assert any(
        isinstance(record.msg, dict)
        and record.msg.get("action") == "generation_flow_reset"
        and record.msg.get("reason") == "test_reset"
        and record.msg.get("state") == GenerationStates.waiting_for_prompt.state
        and record.msg.get("user_id") == 999
        and record.msg.get("incoming_text_type") == "system"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_cancel_command_resets_generation_flow_and_shows_main_menu(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState({"input_media": {"type": "image", "count": 1}, "input_media_items": []})
        state.state = GenerationStates.waiting_for_prompt
        message = FakeMessage(user_id=701, text="/cancel")

        await start.menu_command(message, state, session)

        assert state.state is None
        assert state.data == {}
        assert message.answers[0] == t("generation.scenario_reset", "ru")
        assert message.answers[1] == t("main.choose_section", "ru")
        _assert_main_menu_keyboard(message.answer_markups[0])
        _assert_main_menu_keyboard(message.answer_markups[1])


@pytest.mark.asyncio
async def test_start_command_always_shows_main_menu(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState()
        message = FakeMessage(user_id=704, text="/start")

        await start.start_command(message, state, session)

        assert "Привет" in message.answers[-1]
        _assert_main_menu_keyboard(message.answer_markups[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language_code", "expected_lang", "expected_greeting"),
    [
        ("ru", "ru", "Привет"),
        ("en", "en", "Hi"),
        ("xx", "en", "Hi"),
    ],
)
async def test_start_command_uses_localized_main_menu(
    session_factory,
    language_code: str,
    expected_lang: str,
    expected_greeting: str,
) -> None:
    async with session_factory() as session:
        state = FakeState()
        message = FakeMessage(user_id=710, text="/start", language_code=language_code)

        await start.start_command(message, state, session)

        assert expected_greeting in message.answers[-1]
        _assert_main_menu_keyboard(message.answer_markups[-1], expected_lang)


@pytest.mark.asyncio
async def test_start_command_persists_telegram_language_code(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState()
        message = FakeMessage(user_id=706, text="/start")
        message.from_user.language_code = "pt-BR"

        await start.start_command(message, state, session)

        result = await session.execute(select(User.language_code).where(User.id == 706))
        assert result.scalar_one() == "pt-BR"


@pytest.mark.asyncio
async def test_start_paid_payload_checks_payment_without_crediting_pending_order(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=711, balance=5))
        await session.commit()
        order = await PaymentService(session).create_stars_order(user_id=711, stars_amount=100)
        state = FakeState()
        message = FakeMessage(user_id=711, text=f"/start paid_{order.payload}")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args=f"paid_{order.payload}"))

        result = await session.execute(select(User.balance).where(User.id == 711))
        assert result.scalar_one() == 5
        assert len(message.answers) == 1
        assert message.answers[0].startswith("👤 <b>Профиль</b>")
        assert "Баланс: 5" in message.answers[0]
        assert "Проверяем оплату" not in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_direct_stars_payload_checks_payment_without_crediting(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=717, balance=8))
        await session.commit()
        order = await PaymentService(session).create_stars_order(user_id=717, stars_amount=300)
        state = FakeState()
        message = FakeMessage(user_id=717, text=f"/start {order.payload}")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args=order.payload))

        result = await session.execute(select(User.balance).where(User.id == 717))
        assert result.scalar_one() == 8
        assert len(message.answers) == 1
        assert message.answers[0].startswith("👤 <b>Профиль</b>")
        assert "Баланс: 8" in message.answers[0]
        assert "Проверяем оплату" not in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_stars_prefixed_payload_shows_profile(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=719, balance=9))
        await session.commit()
        order = await PaymentService(session).create_stars_order(user_id=719, stars_amount=300)
        state = FakeState({"payment_screen": "stars_redirect"})
        message = FakeMessage(user_id=719, text=f"/start stars_{order.payload}")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args=f"stars_{order.payload}"))

        result = await session.execute(select(User.balance).where(User.id == 719))
        assert result.scalar_one() == 9
        assert state.state is None
        assert state.data == {}
        assert len(message.answers) == 1
        assert message.answers[0].startswith("👤 <b>Профиль</b>")
        assert "Баланс: 9" in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_paid_payload_decodes_wallet_return_payload(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=718, balance=12))
        await session.commit()
        order = await PaymentService(session).create_stars_order(user_id=718, stars_amount=100)
        order.payload = "stars:legacy:token"
        await session.commit()
        state = FakeState()
        message = FakeMessage(user_id=718, text="/start paid_stars%3Alegacy%3Atoken")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args="paid_stars%3Alegacy%3Atoken"))

        result = await session.execute(select(User.balance).where(User.id == 718))
        assert result.scalar_one() == 12
        assert len(message.answers) == 1
        assert message.answers[0].startswith("👤 <b>Профиль</b>")
        assert "Баланс: 12" in message.answers[0]
        assert "Проверяем оплату" not in message.answers[0]


@pytest.mark.asyncio
async def test_start_paid_payload_does_not_trust_unknown_payload(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=714, balance=33))
        await session.commit()
        state = FakeState()
        message = FakeMessage(user_id=714, text="/start paid_not-a-real-payment")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args="paid_not-a-real-payment"))

        result = await session.execute(select(User.balance).where(User.id == 714))
        assert result.scalar_one() == 33
        assert len(message.answers) == 1
        assert "Привет" in message.answers[0]
        assert "Проверяем оплату" not in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_ref_payload_for_new_user_applies_referral(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "referral_referrer_bonus_credits", 0)
    monkeypatch.setattr(settings, "referral_referred_bonus_credits", 2)
    async with session_factory() as session:
        session.add(User(id=730, balance=5, referral_code="valid730"))
        await session.commit()
        state = FakeState()
        message = FakeMessage(user_id=731, text="/start ref_valid730")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args="ref_valid730"))

        referred_user = await session.get(User, 731)
        assert referred_user is not None
        assert referred_user.referred_by_user_id == 730
        assert referred_user.referred_at is not None
        assert len(message.answers) == 1
        assert "Реферальная ссылка применена" in message.answers[0]
        assert "🎁 Бонус начислен: +2 кредитов" in message.answers[0]
        assert "Привет" in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_ref_payload_for_existing_user_does_not_apply_referral(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=732, balance=5, referral_code="valid732"),
                User(id=733, balance=5, referral_code="user733"),
            ]
        )
        await session.commit()
        state = FakeState()
        message = FakeMessage(user_id=733, text="/start ref_valid732")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args="ref_valid732"))

        referred_user = await session.get(User, 733)
        rejected_event = (await session.execute(select(ReferralEvent))).scalars().one()
        assert referred_user is not None
        assert referred_user.referred_by_user_id is None
        assert rejected_event.status == "rejected"
        assert rejected_event.reject_reason == "already_registered"
        assert len(message.answers) == 1
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "Привет" in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_ref_payload_rejects_own_code(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=734, balance=5, referral_code="own734"))
        await session.commit()
        state = FakeState()
        message = FakeMessage(user_id=734, text="/start ref_own734")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args="ref_own734"))

        referred_user = await session.get(User, 734)
        rejected_event = (await session.execute(select(ReferralEvent))).scalars().one()
        assert referred_user is not None
        assert referred_user.referred_by_user_id is None
        assert rejected_event.status == "rejected"
        assert rejected_event.reject_reason == "self_referral"
        assert len(message.answers) == 1
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "Привет" in message.answers[0]


@pytest.mark.asyncio
async def test_start_payment_payload_still_uses_payment_return_priority(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=735, balance=5, referral_code="ref735"))
        await session.commit()
        order = await PaymentService(session).create_stars_order(user_id=735, stars_amount=100)
        state = FakeState()
        message = FakeMessage(user_id=735, text=f"/start paid_{order.payload}")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args=f"paid_{order.payload}"))

        referral_event_count = (await session.execute(select(func.count()).select_from(ReferralEvent))).scalar_one()
        assert referral_event_count == 0
        assert len(message.answers) == 1
        assert message.answers[0].startswith("👤 <b>Профиль</b>")


@pytest.mark.asyncio
async def test_ordinary_start_still_shows_welcome(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState()
        message = FakeMessage(user_id=736, text="/start")

        await start.start_command(message, state, session)

        assert len(message.answers) == 1
        assert "Привет" in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_paid_payload_does_not_confirm_another_users_paid_order(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([User(id=715, balance=5), User(id=716, balance=20)])
        await session.commit()
        service = PaymentService(session)
        order = await service.create_stars_order(user_id=715, stars_amount=100)
        await service.mark_external_stars_payment_paid(order.payload, "wallet-payment-715")
        state = FakeState()
        message = FakeMessage(user_id=716, text=f"/start paid_{order.payload}")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args=f"paid_{order.payload}"))

        owner_balance = (await session.execute(select(User.balance).where(User.id == 715))).scalar_one()
        requester_balance = (await session.execute(select(User.balance).where(User.id == 716))).scalar_one()
        assert owner_balance == 105
        assert requester_balance == 20
        assert len(message.answers) == 1
        assert "Привет" in message.answers[0]
        assert "Проверяем оплату" not in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_paid_payload_reports_confirmed_external_payment(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=712, balance=5))
        await session.commit()
        service = PaymentService(session)
        order = await service.create_stars_order(user_id=712, stars_amount=100)
        await service.mark_external_stars_payment_paid(order.payload, "wallet-payment-712")
        state = FakeState()
        message = FakeMessage(user_id=712, text=f"/start paid_{order.payload}")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args=f"paid_{order.payload}"))

        result = await session.execute(select(User.balance).where(User.id == 712))
        assert result.scalar_one() == 105
        assert len(message.answers) == 1
        assert message.answers[0].startswith("👤 <b>Профиль</b>")
        assert "Баланс: 105" in message.answers[0]
        assert "Проверяем оплату" not in message.answers[0]
        _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
async def test_start_paid_payload_logs_profile_shown(session_factory, caplog) -> None:
    async with session_factory() as session:
        session.add(User(id=720, balance=5))
        await session.commit()
        service = PaymentService(session)
        order = await service.create_stars_order(user_id=720, stars_amount=100)
        await service.mark_external_stars_payment_paid(order.payload, "wallet-payment-720")
        state = FakeState()
        message = FakeMessage(user_id=720, text=f"/start paid_{order.payload}")

        with caplog.at_level(logging.INFO):
            await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args=f"paid_{order.payload}"))

        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "stars_wallet_return_profile_shown"
            and record.msg.get("user_id") == 720
            and record.msg.get("order_id") == str(order.id)
            and record.msg.get("order_status") == PaymentOrderStatus.PAID.value
            for record in caplog.records
        )


@pytest.mark.asyncio
async def test_start_payment_return_does_not_credit_user_again(session_factory, monkeypatch) -> None:
    async with session_factory() as session:
        session.add(User(id=721, balance=10))
        await session.commit()
        service = PaymentService(session)
        order = await service.create_stars_order(user_id=721, stars_amount=100)
        await service.mark_external_stars_payment_paid(order.payload, "wallet-payment-721")

        async def forbidden_credit_user_for_paid_order(*args, **kwargs):
            raise AssertionError("/start payment return must not credit users")

        monkeypatch.setattr(PaymentService, "credit_user_for_paid_order", forbidden_credit_user_for_paid_order)
        state = FakeState()
        message = FakeMessage(user_id=721, text=f"/start paid_{order.payload}")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args=f"paid_{order.payload}"))

        result = await session.execute(select(User.balance).where(User.id == 721))
        assert result.scalar_one() == 110


@pytest.mark.asyncio
async def test_start_payment_success_shows_refreshed_profile_without_crediting(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=713, balance=205))
        await session.commit()
        state = FakeState()
        message = FakeMessage(user_id=713, text="/start payment_success")

        await start.start_payment_return_command(message, state, session, command=SimpleNamespace(args="payment_success"))

        result = await session.execute(select(User.balance).where(User.id == 713))
        assert result.scalar_one() == 205
        assert len(message.answers) == 1
        assert message.answers[0].startswith("👤 <b>Профиль</b>")
        assert "Баланс: 205" in message.answers[0]


def test_payment_return_handler_registered_before_ordinary_start_handler() -> None:
    message_handlers = start.router.observers["message"].handlers
    callback_names = [handler.callback.__name__ for handler in message_handlers]

    assert callback_names.index("start_payment_return_command") < callback_names.index("start_command")


@pytest.mark.asyncio
async def test_menu_command_always_shows_main_menu(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState()
        message = FakeMessage(user_id=705, text="/menu")

        await start.menu_command(message, state, session)

        assert message.answers[-1] == t("main.choose_section", "ru")
        _assert_main_menu_keyboard(message.answer_markups[-1])


@pytest.mark.asyncio
async def test_generations_button_resets_generation_flow_before_opening_menu() -> None:
    state = FakeState({"input_media": {"type": "image", "count": 1}, "input_media_items": []})
    state.state = GenerationStates.waiting_for_image
    message = FakeMessage(user_id=702, text="🎨 Генерации")

    await generations.show_generation_menu(message, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert message.answers[0] == t("generation.scenario_reset", "ru")
    assert f"{t('generation.choose_type', 'ru')}:" in message.answers[1]
    _assert_main_menu_keyboard(message.answer_markups[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "handler", "expected_text"),
    [
        ("👤 Профиль", profile.show_profile, "👤 <b>Профиль</b>"),
    ],
)
async def test_main_menu_buttons_reset_generation_flow_before_navigation(
    session_factory,
    label: str,
    handler,
    expected_text: str,
) -> None:
    async with session_factory() as session:
        state = FakeState({"input_media": {"type": "image", "count": 1}, "input_media_items": []})
        state.state = GenerationStates.waiting_for_prompt
        message = FakeMessage(user_id=703, text=label)

        await handler(message, state, session)

        assert state.state is None
        assert state.data == {}
        assert message.answers[0] == t("generation.scenario_reset", "ru")
        _assert_main_menu_keyboard(message.answer_markups[0])
        assert expected_text in message.answers[1]