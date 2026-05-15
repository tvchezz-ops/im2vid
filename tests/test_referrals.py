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
from app.utils.referrals import generate_referral_code
from scripts.audit_referrals import audit_referrals


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
        self.edits: list[str] = []
        self.edit_markups: list[object] = []

    async def answer(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.answers.append(text)
        self.answer_markups.append(reply_markup)

    async def edit_text(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.edits.append(text)
        self.edit_markups.append(reply_markup)


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


def test_referral_link_generation_works() -> None:
    assert build_referral_link("@example_bot", "Abc123xy") == "https://t.me/example_bot?start=ref_Abc123xy"


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
async def test_ensure_referral_code_assigns_missing_code(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=1003, referral_code=None))
        await session.commit()

        code = await UserRepository(session).ensure_referral_code(1003)

        assert code is not None
        assert len(code) == 8


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
        assert "👤 <b>Профиль</b>" in profile_message.answers[-1]
        assert f"https://t.me/imai_test_bot?start=ref_{user_a.referral_code}" in invite_message.edits[-1]

        start_message = FakeStartMessage(user_id=1402, text=f"/start ref_{user_a.referral_code}")
        await start.start_payment_return_command(
            start_message,
            state,
            session,
            command=SimpleNamespace(args=f"ref_{user_a.referral_code}"),
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
        assert user_b.referred_by_user_id == user_a.id
        assert accepted_event.referrer_user_id == user_a.id
        assert "Реферальная ссылка применена" in start_message.answers[0]


@pytest.mark.asyncio
async def test_flow_scenario_b_own_referral_link_rejects_self_without_ui_spam(session_factory) -> None:
    async with session_factory() as session:
        session.add(User(id=1403, referral_code="self1403"))
        await session.commit()
        message = FakeStartMessage(user_id=1403, text="/start ref_self1403")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args="ref_self1403"),
        )

        user = await session.get(User, 1403)
        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert user is not None
        assert user.referred_by_user_id is None
        assert event.status == ReferralEventStatus.REJECTED.value
        assert event.reject_reason == "self_referral"
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "self_referral" not in message.answers[0]
        assert "Привет" in message.answers[0]


@pytest.mark.asyncio
async def test_flow_scenario_c_existing_user_rejected_already_registered(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([User(id=1404, referral_code="ref1404"), User(id=1405, referral_code="user1405")])
        await session.commit()
        message = FakeStartMessage(user_id=1405, text="/start ref_ref1404")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args="ref_ref1404"),
        )

        user_b = await session.get(User, 1405)
        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert user_b is not None
        assert user_b.referred_by_user_id is None
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
                User(id=1407, referral_code="ref1407"),
                User(id=1408, referral_code="user1408", referred_by_user_id=1406),
            ]
        )
        await session.commit()
        message = FakeStartMessage(user_id=1408, text="/start ref_ref1407")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args="ref_ref1407"),
        )

        user_b = await session.get(User, 1408)
        event = (await session.execute(sa.select(ReferralEvent))).scalars().one()
        assert user_b is not None
        assert user_b.referred_by_user_id == 1406
        assert event.referrer_user_id == 1407
        assert event.status == ReferralEventStatus.REJECTED.value
        assert event.reject_reason == "already_referred"
        assert "Реферальная ссылка применена" not in message.answers[0]
        assert "Привет" in message.answers[0]


@pytest.mark.asyncio
async def test_flow_scenario_e_invalid_code_rejected_with_normal_welcome(session_factory) -> None:
    async with session_factory() as session:
        message = FakeStartMessage(user_id=1409, text="/start ref_missing")

        await start.start_payment_return_command(
            message,
            FakeState(),
            session,
            command=SimpleNamespace(args="ref_missing"),
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
        assert referrer.balance == 5
        assert user.balance == 5
        transaction_count = (await session.execute(sa.select(sa.func.count()).select_from(CreditTransaction))).scalar_one()
        assert transaction_count == 0


@pytest.mark.asyncio
async def test_referral_bonus_disabled_by_default_creates_no_transactions(session_factory, monkeypatch) -> None:
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
async def test_referral_bonus_enabled_credits_both_users_and_records_transactions(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(settings, "referral_referrer_bonus_credits", 5)
    monkeypatch.setattr(settings, "referral_referred_bonus_credits", 2)
    async with session_factory() as session:
        referrer = User(id=1314, referral_code="ref1314", balance=10)
        user = User(id=1315, referral_code="user1315", balance=3)
        user.newly_created_user = True
        session.add_all([referrer, user])
        await session.commit()

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
