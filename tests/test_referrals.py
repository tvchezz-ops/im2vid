from __future__ import annotations

import os
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")

from app.db.base import Base
from app.config import settings
from app.bot.routers import profile, start
from app.bot.routers.profile import build_referral_link
from app.db.models import CreditTransaction, ReferralEvent, ReferralEventStatus, User
from app.db.repositories import UserRepository
from app.services.referrals import ReferralService
from app.utils.referrals import MAX_REFERRAL_CODE_LENGTH, generate_referral_code, generate_start_payload
from scripts.audit_referrals import audit_referrals
from scripts.backfill_referral_bonuses import backfill_referral_bonuses


class FakeStartMessage:
    def __init__(self, user_id: int = 1, text: str | None = None):
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
        self.text = text
        self.answers: list[str] = []
        self.answer_markups: list[object] = []
        self.answer_kwargs: list[dict[str, object]] = []
        self.edits: list[str] = []
        self.edit_markups: list[object] = []
        self.edit_kwargs: list[dict[str, object]] = []

    async def answer(self, text: str, reply_markup=None, parse_mode=None, **kwargs) -> None:
        self.answers.append(text)
        self.answer_markups.append(reply_markup)
        self.answer_kwargs.append({"parse_mode": parse_mode, **kwargs})

    async def edit_text(self, text: str, reply_markup=None, parse_mode=None, **kwargs) -> None:
        self.edits.append(text)
        self.edit_markups.append(reply_markup)
        self.edit_kwargs.append({"parse_mode": parse_mode, **kwargs})


class FakeCallback:
    def __init__(self, user_id: int, message: FakeStartMessage, data: str):
        self.from_user = SimpleNamespace(
            id=user_id,
            username="tester",
            first_name="Test",
            last_name=None,
            language_code="ru",
            is_bot=False,
            is_premium=False,
        )
        self.message = message
        self.data = data
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeState:
    state = None

    async def get_state(self):
        return self.state

    async def update_data(self, **kwargs) -> None:
        return None

    async def clear(self) -> None:
        self.state = None


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "referrals.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


def test_generate_referral_code_is_short_and_url_safe() -> None:
    code = generate_referral_code()

    assert len(code) == 8
    assert code.isalnum()


def test_referral_code_columns_allow_longer_codes() -> None:
    assert User.__table__.c.referral_code.type.length == MAX_REFERRAL_CODE_LENGTH
    assert ReferralEvent.__table__.c.referral_code.type.length == MAX_REFERRAL_CODE_LENGTH


def test_generate_referral_code_rejects_codes_longer_than_schema() -> None:
    with pytest.raises(ValueError, match="at most 64"):
        generate_referral_code(MAX_REFERRAL_CODE_LENGTH + 1)


def test_generate_start_payload_is_random_url_safe_and_has_no_prefix() -> None:
    payload = generate_start_payload()

    assert 10 <= len(payload) <= 24
    assert payload.isalnum()
    assert not payload.startswith(("ref_", "referral_", "paid_", "stars_", "pay_"))


def test_referral_link_generation_works() -> None:
    assert build_referral_link("@example_bot", "X7pQ2Lm9Ka") == "https://t.me/example_bot?start=X7pQ2Lm9Ka"


def test_payment_payload_is_not_treated_as_referral() -> None:
    assert start._extract_referral_code("paid_order-payload") is None
    assert start._extract_referral_code("stars_order-payload") is None
    assert start._extract_referral_code("payment_success") is None


@pytest.mark.asyncio
async def test_referral_code_is_unique(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1001, referral_code="Abc123xy"),
                User(id=1002, referral_code="Abc123xy"),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_start_payload_is_unique(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1005, referral_code="code1005", start_payload="X7pQ2Lm9Ka"),
                User(id=1006, referral_code="code1006", start_payload="X7pQ2Lm9Ka"),
            ]
        )

        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_ensure_referral_code_assigns_missing_code(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=1003, referral_code=None))
        await session.commit()

        code = await UserRepository(session).ensure_referral_code(1003)

        assert code is not None
        assert len(code) == 8


@pytest.mark.asyncio
async def test_ensure_start_payload_assigns_missing_payload(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=1007, referral_code="code1007", start_payload=None))
        await session.commit()

        payload = await UserRepository(session).ensure_start_payload(1007)

        assert payload is not None
        assert 10 <= len(payload) <= 24
        assert payload.isalnum()
        assert not payload.startswith(("ref_", "referral_"))


@pytest.mark.asyncio
async def test_user_cannot_refer_self_at_database_level(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=1004, referral_code="self1004", referred_by_user_id=1004))

        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_user_can_have_only_one_accepted_referral(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1101, referral_code="ref1101"),
                User(id=1102, referral_code="ref1102"),
                User(id=1103, referral_code="ref1103"),
            ]
        )
        await session.commit()

        session.add(
            ReferralEvent(
                referrer_user_id=1201,
                referred_user_id=1202,
                referral_code="bad-code",
                status=ReferralEventStatus.REJECTED.value,
            )
        )
        await session.commit()

        session.add(
            ReferralEvent(
                referrer_user_id=1201,
                referred_user_id=1202,
                referral_code="ref1201",
                status=ReferralEventStatus.ACCEPTED.value,
            )
        )
        await session.commit()

        session.add(
            ReferralEvent(
                referrer_user_id=1101,
                referred_user_id=1103,
                referral_code="ref1101",
                status=ReferralEventStatus.ACCEPTED.value,
            )
        )
        await session.commit()

        session.add(
            ReferralEvent(
                referrer_user_id=1102,
                referred_user_id=1103,
                referral_code="ref1102",
                status=ReferralEventStatus.ACCEPTED.value,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_rejected_referral_events_do_not_block_later_acceptance(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1201, referral_code="ref1201"),
                User(id=1202, referral_code="ref1202"),
            ]
        )
        await session.commit()

        session.add(
            ReferralEvent(
                referrer_user_id=None,
                referred_user_id=1202,
                referral_code="missing-code",
                status=ReferralEventStatus.REJECTED.value,
                reject_reason="invalid_code",
            )
        )
        await session.commit()
        user = await session.get(User, 1202)
        assert user is not None
        user.newly_created_user = True

        result = await ReferralService(session).apply_referral(user, "ref1201")

        events = (await session.execute(sa.select(ReferralEvent).order_by(ReferralEvent.status))).scalars().all()
        await session.refresh(user)
        assert result.status == "accepted"
        assert user.referred_by_user_id == 1201
        assert sorted(event.status for event in events) == ["accepted", "rejected"]


@pytest.mark.asyncio
async def test_apply_referral_rejects_invalid_code(session_factory) -> None:
    async with session_factory() as session:
        user = User(id=1301, referral_code="user1301")
        user.newly_created_user = True
        session.add(user)
        await session.commit()

        result = await ReferralService(session).apply_referral(user, "missing-code")

        assert result.status == "rejected"
        assert result.reason == "invalid_code"
        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert event.status == "rejected"
        assert event.reject_reason == "invalid_code"


@pytest.mark.asyncio
async def test_apply_referral_without_code_does_nothing(session_factory) -> None:
    async with session_factory() as session:
        user = User(id=1300, referral_code="user1300")
        user.newly_created_user = True
        session.add(user)
        await session.commit()

        result = await ReferralService(session).apply_referral(user, None)

        assert result.status == "none"
        await session.refresh(user)
        assert user.referred_by_user_id is None
        event_count = (await session.execute(sa.select(sa.func.count()).select_from(ReferralEvent))).scalar_one()
        assert event_count == 0


@pytest.mark.asyncio
async def test_referral_rejection_logs_reason_but_welcome_ui_stays_clean(session_factory, caplog) -> None:
    async with session_factory() as session:
        user = User(id=1316, referral_code="user1316")
        user.newly_created_user = True
        session.add(user)
        await session.commit()

        with caplog.at_level(logging.INFO):
            result = await ReferralService(session).apply_referral(user, "missing-code")

        message = FakeStartMessage(user_id=1316)
        await start._send_referral_start_welcome(
            message,
            user,
            "en",
            accepted=result.status == "accepted",
            bonus_credits=result.referred_bonus_credits,
        )

        assert result.status == "rejected"
        assert result.reason == "invalid_code"
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "referral_rejected"
            and record.msg.get("reason") == "invalid_code"
            for record in caplog.records
        )
        assert len(message.answers) == 1
        assert "Referral" not in message.answers[0]
        assert "invalid_code" not in message.answers[0]
        assert "missing-code" not in message.answers[0]


@pytest.mark.asyncio
async def test_flow_scenario_a_profile_link_creates_user_b_and_accepts_referral(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "main_bot_username", "imai_test_bot")
    async with session_factory() as session:
        state = FakeState()
        profile_message = FakeStartMessage(user_id=1401)

        await profile.show_profile(profile_message, state, session)
        invite_message = FakeStartMessage(user_id=1401)
        invite_callback = FakeCallback(user_id=1401, message=invite_message, data="profile:invite_friends")
        await profile.show_referral_invite(invite_callback, session)

        user_a = await session.get(User, 1401)
        assert user_a is not None
        assert user_a.referral_code is not None
        assert user_a.start_payload is not None
        assert "👤 <b>Профиль</b>" in profile_message.answers[-1]
        assert f"https://t.me/imai_test_bot?start={user_a.start_payload}" in invite_message.edits[-1]
        assert f"ref_{user_a.referral_code}" not in invite_message.edits[-1]

        start_message = FakeStartMessage(user_id=1402, text=f"/start {user_a.start_payload}")
        await start.start_payment_return_command(
            start_message,
            state,
            session,
            command=SimpleNamespace(args=user_a.start_payload),
        )

        user_b = await session.get(User, 1402)
        accepted_event = (
            await session.execute(
                sa.select(ReferralEvent).where(
                    ReferralEvent.referred_user_id == 1402,
                    ReferralEvent.status == ReferralEventStatus.ACCEPTED.value,
                )
            )
        ).scalars().one()
        assert user_b is not None
        await session.refresh(user_a)
        assert user_b.referred_by_user_id == user_a.id
        assert user_a.balance == 15
        assert user_b.balance == 10
        assert await UserRepository(session).count_accepted_referrals(user_a.id) == 1
        assert accepted_event.referrer_user_id == user_a.id
        transaction = (await session.execute(sa.select(CreditTransaction))).scalars().one()
        assert transaction.type == "referral_bonus"
        assert transaction.user_id == user_a.id
        assert transaction.amount == 5
        assert "Реферальная ссылка применена" in start_message.answers[0]


@pytest.mark.asyncio
async def test_start_legacy_referral_code_longer_than_10_is_saved_to_referral_events(session_factory) -> None:
    async with session_factory() as session:
        long_referral_code = "hFvuVUKtjcIl"
        session.add(User(id=1420, referral_code=long_referral_code, start_payload="X7pQ2Lm9Ka22"))
        await session.commit()
        message = FakeStartMessage(user_id=1421, text=f"/start ref_{long_referral_code}")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args=f"ref_{long_referral_code}"),
        )

        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert event.status == ReferralEventStatus.ACCEPTED.value
        assert event.referral_code == long_referral_code
        assert "Реферальная ссылка применена" in message.answers[0]


@pytest.mark.asyncio
async def test_flow_scenario_b_own_referral_link_rejects_self_without_ui_spam(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=1403, referral_code="self1403", start_payload="X7pQ2Lm9Ka"))
        await session.commit()
        message = FakeStartMessage(user_id=1403, text="/start X7pQ2Lm9Ka")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args="X7pQ2Lm9Ka"),
        )

        user = await session.get(User, 1403)
        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert user is not None
        assert user.referred_by_user_id is None
        assert user.balance == 10
        assert event.status == ReferralEventStatus.REJECTED.value
        assert event.reject_reason == "self_referral"
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "self_referral" not in message.answers[0]
        assert "Привет" in message.answers[0]


@pytest.mark.asyncio
async def test_flow_scenario_c_existing_user_rejected_already_registered(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1404, referral_code="ref1404", start_payload="p9QaLm2Xv81"),
                User(id=1405, referral_code="user1405"),
            ]
        )
        await session.commit()
        message = FakeStartMessage(user_id=1405, text="/start p9QaLm2Xv81")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args="p9QaLm2Xv81"),
        )

        user_b = await session.get(User, 1405)
        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert user_b is not None
        assert user_b.referred_by_user_id is None
        assert user_b.balance == 10
        assert event.status == ReferralEventStatus.REJECTED.value
        assert event.reject_reason == "already_registered"
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "Привет" in message.answers[0]


@pytest.mark.asyncio
async def test_flow_scenario_d_already_referred_user_keeps_original_referrer(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1406, referral_code="ref1406"),
                User(id=1407, referral_code="ref1407", start_payload="aK82PqLmN0sD"),
                User(id=1408, referral_code="user1408", referred_by_user_id=1406),
            ]
        )
        await session.commit()
        message = FakeStartMessage(user_id=1408, text="/start aK82PqLmN0sD")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args="aK82PqLmN0sD"),
        )

        user_b = await session.get(User, 1408)
        referrer_a = await session.get(User, 1406)
        referrer_c = await session.get(User, 1407)
        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert user_b is not None
        assert referrer_a is not None
        assert referrer_c is not None
        assert user_b.referred_by_user_id == 1406
        assert referrer_a.balance == 10
        assert referrer_c.balance == 10
        assert event.referrer_user_id == 1407
        assert event.status == ReferralEventStatus.REJECTED.value
        assert event.reject_reason == "already_referred"
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "Привет" in message.answers[0]


@pytest.mark.asyncio
async def test_flow_scenario_e_invalid_code_rejected_with_normal_welcome(session_factory) -> None:
    async with session_factory() as session:
        message = FakeStartMessage(user_id=1409, text="/start MissingToken9")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args="MissingToken9"),
        )

        user = await session.get(User, 1409)
        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert user is not None
        assert user.referred_by_user_id is None
        assert event.status == ReferralEventStatus.REJECTED.value
        assert event.reject_reason == "invalid_code"
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "invalid_code" not in message.answers[0]
        assert "Привет" in message.answers[0]


@pytest.mark.asyncio
async def test_start_referral_service_exception_logs_rolls_back_and_shows_normal_welcome(session_factory, monkeypatch, caplog) -> None:
    async def fail_apply_referral(self, new_user, referral_code, *, created=None, referrer=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(ReferralService, "apply_referral", fail_apply_referral)
    async with session_factory() as session:
        session.add(User(id=1422, referral_code="ref1422", start_payload="Xa82PqLmN0sD"))
        await session.commit()
        message = FakeStartMessage(user_id=1423, text="/start Xa82PqLmN0sD")

        with caplog.at_level(logging.ERROR):
            await start.start_payment_return_command(
                message,
                FakeState(),
                session,
                command=SimpleNamespace(args="Xa82PqLmN0sD"),
            )

        event_count = (await session.execute(sa.select(sa.func.count()).select_from(ReferralEvent))).scalar_one()
        user = await session.get(User, 1423)
        assert event_count == 0
        assert user is not None
        assert user.referred_by_user_id is None
        assert len(message.answers) == 1
        assert "Привет" in message.answers[0]
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "ошибка" not in message.answers[0].lower()
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "referral_start_flow_failed"
            and record.msg.get("error_code") == "REFERRAL_START_FLOW_ERROR"
            for record in caplog.records
        )


@pytest.mark.asyncio
async def test_oversized_start_payload_is_rejected_before_lookup(session_factory, caplog) -> None:
    async with session_factory() as session:
        oversized_payload = "A" * 65
        message = FakeStartMessage(user_id=1410, text=f"/start {oversized_payload}")

        with caplog.at_level(logging.INFO):
            await start.start_payment_return_command(
                message,
                FakeState(),
                session,
                command=SimpleNamespace(args=oversized_payload),
            )

        event_count = (await session.execute(sa.select(sa.func.count()).select_from(ReferralEvent))).scalar_one()
        assert event_count == 0
        assert "Привет" in message.answers[0]
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "referral_payload_rejected"
            and record.msg.get("reason") == "oversized_payload"
            and record.msg.get("payload_prefix") == "AAA***"
            for record in caplog.records
        )
        assert all(oversized_payload not in str(record.msg) for record in caplog.records)


@pytest.mark.asyncio
async def test_apply_referral_rejects_self_referral(session_factory) -> None:
    async with session_factory() as session:
        user = User(id=1302, referral_code="self1302")
        user.newly_created_user = True
        session.add(user)
        await session.commit()

        result = await ReferralService(session).apply_referral(user, "self1302")

        assert result.status == "rejected"
        assert result.reason == "self_referral"


@pytest.mark.asyncio
async def test_apply_referral_rejects_already_referred_user(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1303, referral_code="ref1303"),
                User(id=1304, referral_code="ref1304"),
                User(id=1305, referral_code="user1305", referred_by_user_id=1303),
            ]
        )
        await session.commit()
        user = await session.get(User, 1305)
        assert user is not None
        user.newly_created_user = True

        result = await ReferralService(session).apply_referral(user, "ref1304")

        assert result.status == "rejected"
        assert result.reason == "already_referred"
        await session.refresh(user)
        assert user.referred_by_user_id == 1303


@pytest.mark.asyncio
async def test_apply_referral_rejects_existing_registered_user_without_referrer(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1306, referral_code="ref1306"),
                User(id=1307, referral_code="user1307"),
            ]
        )
        await session.commit()
        user = await session.get(User, 1307)
        assert user is not None
        user.newly_created_user = False

        result = await ReferralService(session).apply_referral(user, "ref1306")

        assert result.status == "rejected"
        assert result.reason == "already_registered"
        await session.refresh(user)
        assert user.referred_by_user_id is None


@pytest.mark.asyncio
async def test_apply_referral_accepts_and_sets_referrer(session_factory) -> None:
    async with session_factory() as session:
        referrer = User(id=1308, referral_code="ref1308", balance=5)
        user = User(id=1309, referral_code="user1309", balance=5)
        user.newly_created_user = True
        session.add_all([referrer, user])
        await session.commit()

        result = await ReferralService(session).apply_referral(user, "ref1308")

        assert result.status == "accepted"
        assert result.referrer_user_id == 1308
        await session.refresh(user)
        await session.refresh(referrer)
        assert user.referred_by_user_id == 1308
        assert user.referred_at is not None
        assert referrer.balance == 10
        assert user.balance == 5
        transaction = (await session.execute(sa.select(CreditTransaction))).scalars().one()
        event = (await session.execute(sa.select(ReferralEvent).where(ReferralEvent.status == "accepted"))).scalars().one()
        assert transaction.type == "referral_bonus"
        assert transaction.user_id == 1308
        assert transaction.amount == 5
        assert transaction.referral_event_id == event.id
        assert transaction.metadata_["referred_user_id"] == 1309
        assert transaction.metadata_["referral_event_id"] == str(event.id)


@pytest.mark.asyncio
async def test_referral_bonus_disabled_by_config_creates_no_transactions(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "referral_referrer_bonus_credits", 0)
    monkeypatch.setattr(settings, "referral_referred_bonus_credits", 0)
    async with session_factory() as session:
        referrer = User(id=1312, referral_code="ref1312", balance=5)
        user = User(id=1313, referral_code="user1313", balance=5)
        user.newly_created_user = True
        session.add_all([referrer, user])
        await session.commit()

        result = await ReferralService(session).apply_referral(user, "ref1312")

        await session.refresh(referrer)
        await session.refresh(user)
        transaction_count = (await session.execute(sa.select(sa.func.count()).select_from(CreditTransaction))).scalar_one()
        assert result.status == "accepted"
        assert result.referrer_bonus_credits == 0
        assert result.referred_bonus_credits == 0
        assert referrer.balance == 5
        assert user.balance == 5
        assert transaction_count == 0


@pytest.mark.asyncio
async def test_referral_bonus_enabled_credits_both_users_and_records_transactions(session_factory, monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "referral_referrer_bonus_credits", 5)
    monkeypatch.setattr(settings, "referral_referred_bonus_credits", 2)
    async with session_factory() as session:
        referrer = User(id=1314, referral_code="ref1314", balance=10)
        user = User(id=1315, referral_code="user1315", balance=3)
        user.newly_created_user = True
        session.add_all([referrer, user])
        await session.commit()

        with caplog.at_level(logging.INFO):
            result = await ReferralService(session).apply_referral(user, "ref1314")

        await session.refresh(referrer)
        await session.refresh(user)
        transactions = (await session.execute(sa.select(CreditTransaction).order_by(CreditTransaction.user_id))).scalars().all()
        event = (await session.execute(sa.select(ReferralEvent).where(ReferralEvent.status == "accepted"))).scalars().one()
        assert result.status == "accepted"
        assert result.referrer_bonus_credits == 5
        assert result.referred_bonus_credits == 2
        assert referrer.balance == 15
        assert user.balance == 5
        assert [(transaction.user_id, transaction.amount, transaction.type) for transaction in transactions] == [
            (1314, 5, "referral_bonus"),
            (1315, 2, "referral_bonus"),
        ]
        assert all(transaction.referral_event_id == event.id for transaction in transactions)
        assert all(transaction.metadata_["referrer_user_id"] == 1314 for transaction in transactions)
        assert all(transaction.metadata_["referred_user_id"] == 1315 for transaction in transactions)
        assert all(transaction.metadata_["referral_event_id"] == str(event.id) for transaction in transactions)
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "referral_bonus_granted"
            and record.msg.get("referrer_user_id") == 1314
            and record.msg.get("referred_user_id") == 1315
            and record.msg.get("credits") == 5
            and record.msg.get("referral_event_id") == str(event.id)
            for record in caplog.records
        )


@pytest.mark.asyncio
async def test_apply_referral_repeated_start_does_not_double_apply(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "referral_referrer_bonus_credits", 5)
    monkeypatch.setattr(settings, "referral_referred_bonus_credits", 2)
    async with session_factory() as session:
        referrer = User(id=1310, referral_code="ref1310", balance=5)
        user = User(id=1311, referral_code="user1311", balance=5)
        user.newly_created_user = True
        session.add_all([referrer, user])
        await session.commit()
        service = ReferralService(session)

        first_result = await service.apply_referral(user, "ref1310")
        second_result = await service.apply_referral(user, "ref1310")

        await session.refresh(referrer)
        await session.refresh(user)
        accepted_events = (
            await session.execute(
                sa.select(sa.func.count()).select_from(ReferralEvent).where(ReferralEvent.status == "accepted")
            )
        ).scalar_one()
        assert first_result.status == "accepted"
        assert second_result.status == "rejected"
        assert second_result.reason == "already_referred"
        assert accepted_events == 1
        assert user.referred_by_user_id == 1310
        assert referrer.balance == 10
        assert user.balance == 7
        transaction_count = (await session.execute(sa.select(sa.func.count()).select_from(CreditTransaction))).scalar_one()
        assert transaction_count == 2


@pytest.mark.asyncio
async def test_backfill_referral_bonuses_adds_missing_bonus_once(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "referral_referrer_bonus_credits", 5)
    async with session_factory() as session:
        referrer = User(id=1324, referral_code="ref1324", balance=5)
        user = User(id=1325, referral_code="user1325", balance=5, referred_by_user_id=1324)
        session.add_all([referrer, user])
        await session.commit()
        event = ReferralEvent(
            referrer_user_id=1324,
            referred_user_id=1325,
            referral_code="ref1324",
            status=ReferralEventStatus.ACCEPTED.value,
        )
        session.add(event)
        await session.commit()

        summary = await backfill_referral_bonuses(session, bonus_credits=5)

        await session.refresh(referrer)
        transactions = (await session.execute(sa.select(CreditTransaction))).scalars().all()
        assert summary.processed == 1
        assert summary.credited == 1
        assert summary.skipped_existing == 0
        assert summary.total_credits_added == 5
        assert referrer.balance == 10
        assert len(transactions) == 1
        assert transactions[0].user_id == 1324
        assert transactions[0].amount == 5
        assert transactions[0].referral_event_id == event.id
        assert transactions[0].metadata_["referred_user_id"] == 1325
        assert transactions[0].metadata_["referral_event_id"] == str(event.id)


@pytest.mark.asyncio
async def test_backfill_referral_bonuses_is_idempotent(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "referral_referrer_bonus_credits", 5)
    async with session_factory() as session:
        referrer = User(id=1326, referral_code="ref1326", balance=5)
        user = User(id=1327, referral_code="user1327", balance=5, referred_by_user_id=1326)
        session.add_all([referrer, user])
        await session.commit()
        event = ReferralEvent(
            referrer_user_id=1326,
            referred_user_id=1327,
            referral_code="ref1326",
            status=ReferralEventStatus.ACCEPTED.value,
        )
        session.add(event)
        await session.commit()

        first_summary = await backfill_referral_bonuses(session, bonus_credits=5)
        second_summary = await backfill_referral_bonuses(session, bonus_credits=5)

        await session.refresh(referrer)
        transaction_count = (await session.execute(sa.select(sa.func.count()).select_from(CreditTransaction))).scalar_one()
        assert first_summary.credited == 1
        assert first_summary.total_credits_added == 5
        assert second_summary.processed == 1
        assert second_summary.credited == 0
        assert second_summary.skipped_existing == 1
        assert second_summary.total_credits_added == 0
        assert referrer.balance == 10
        assert transaction_count == 1


@pytest.mark.asyncio
async def test_referral_audit_passes_for_consistent_data(session_factory) -> None:
    async with session_factory() as session:
        referrer = User(id=1320, referral_code="ref1320")
        user = User(id=1321, referral_code="user1321")
        user.newly_created_user = True
        session.add_all([referrer, user])
        await session.commit()

        result = await ReferralService(session).apply_referral(user, "ref1320")

        assert result.status == "accepted"
        assert await audit_referrals(session) == []


@pytest.mark.asyncio
async def test_referral_audit_reports_accepted_event_user_mismatch(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=1322, referral_code="ref1322"),
                User(id=1323, referral_code="user1323"),
                ReferralEvent(
                    referrer_user_id=1322,
                    referred_user_id=1323,
                    referral_code="ref1322",
                    status=ReferralEventStatus.ACCEPTED.value,
                ),
            ]
        )
        await session.commit()

        issues = await audit_referrals(session)

        assert [(issue.check, issue.count) for issue in issues] == [("accepted_event_user_mismatch", 1)]


def test_referral_migration_applies_cleanly(tmp_path) -> None:
    db_path = tmp_path / "migration.sqlite3"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    metadata = sa.MetaData()
    sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)

    migration_path = Path("alembic/versions/20260515_120000_add_referral_system_backend.py")
    spec = importlib.util.spec_from_file_location("referral_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()

    inspector = sa.inspect(engine)
    assert "referral_events" in inspector.get_table_names()
    assert "credit_transactions" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("users")} >= {
        "referral_code",
        "referred_by_user_id",
        "referred_at",
    }


def test_expand_referral_code_length_migration_applies_cleanly(tmp_path) -> None:
    db_path = tmp_path / "referral-code-length-migration.sqlite3"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    metadata = sa.MetaData()
    sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("referral_code", sa.String(length=10), nullable=True),
    )
    sa.Table(
        "referral_events",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("referral_code", sa.String(length=10), nullable=True),
    )
    metadata.create_all(engine)

    migration_path = Path("alembic/versions/20260515_150000_expand_referral_code_length.py")
    spec = importlib.util.spec_from_file_location("expand_referral_code_length_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()

    inspector = sa.inspect(engine)
    user_referral_code = next(column for column in inspector.get_columns("users") if column["name"] == "referral_code")
    event_referral_code = next(column for column in inspector.get_columns("referral_events") if column["name"] == "referral_code")
    assert user_referral_code["type"].length == MAX_REFERRAL_CODE_LENGTH
    assert event_referral_code["type"].length == MAX_REFERRAL_CODE_LENGTH


def test_start_payload_migration_applies_cleanly(tmp_path) -> None:
    db_path = tmp_path / "start-payload-migration.sqlite3"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    metadata = sa.MetaData()
    sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("referral_code", sa.String(length=10), nullable=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO users (id, created_at, updated_at) VALUES (1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))

    migration_path = Path("alembic/versions/20260515_130000_add_user_start_payload.py")
    spec = importlib.util.spec_from_file_location("start_payload_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()

    inspector = sa.inspect(engine)
    assert "start_payload" in {column["name"] for column in inspector.get_columns("users")}
    assert "ix_users_start_payload" in {index["name"] for index in inspector.get_indexes("users")}
    with engine.connect() as connection:
        payload = connection.execute(sa.text("SELECT start_payload FROM users WHERE id = 1")).scalar_one()
    assert payload is not None
    assert 10 <= len(payload) <= 24
