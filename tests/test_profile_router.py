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


from app.bot.routers import profile
from app.bot.keyboards import get_button_text
from app.bot.states import GenerationStates
from app.db.base import Base
from app.db.models import ReferralEvent, ReferralEventStatus, User
from app.i18n import SUPPORTED_LANGUAGES, t


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
    def __init__(self, user_id: int = 1, message: FakeMessage | None = None, data: str = "profile:toggle_delivery_mode"):
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

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeState:
    def __init__(self):
        self.state = None

    async def get_state(self):
        return self.state

    async def clear(self) -> None:
        self.state = None


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "profile-router.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


@pytest.mark.asyncio
async def test_show_profile_displays_clean_summary_and_delivery_toggle(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState()
        message = FakeMessage(user_id=601)

        await profile.show_profile(message, state, session)

        assert message.answers[-1].startswith("👤 <b>Профиль</b>")
        assert "Username" not in message.answers[-1]
        assert "Имя" not in message.answers[-1]
        assert "Язык" not in message.answers[-1]
        assert "Premium" not in message.answers[-1]
        assert "Дата регистрации" not in message.answers[-1]
        assert "Последняя активность" not in message.answers[-1]
        assert "История" not in message.answers[-1]
        assert f"💳 {t('profile.balance', 'ru')}: 5" in message.answers[-1]
        assert f"🎨 {t('profile.total_generations', 'ru')}: 0" in message.answers[-1]
        assert '🛟 Поддержка: <a href="https://t.me/supbananify">@supbananify</a>' in message.answers[-1]
        assert "🎁 Приглашено: 0" in message.answers[-1]
        assert "📦 Отправка: обычный формат" in message.answers[-1]
        assert message.answers[-1].index("🎨") < message.answers[-1].index("🛟") < message.answers[-1].index("🎁")
        assert "Потрачено" not in message.answers[-1]
        assert "Credits spent" not in message.answers[-1]
        keyboard = message.answer_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == f"💳 {t('profile.top_up', 'ru')}"
        assert keyboard.inline_keyboard[1][0].text == "🎁 Пригласить друзей"
        assert keyboard.inline_keyboard[1][0].callback_data == "profile:invite_friends"
        assert keyboard.inline_keyboard[2][0].text == "📎 Отправлять файлом"
        assert "Настройки" not in "\n".join(button.text for row in keyboard.inline_keyboard for button in row)
        assert len(keyboard.inline_keyboard) == 3
        assert all(row[0].text != "📜 История генераций" for row in keyboard.inline_keyboard)
        assert all("Назад" not in row[0].text for row in keyboard.inline_keyboard)


@pytest.mark.asyncio
async def test_toggle_delivery_mode_updates_profile_message(session_factory) -> None:
    async with session_factory() as session:
        message = FakeMessage(user_id=602)
        callback = FakeCallback(user_id=602, message=message)

        await profile.toggle_delivery_mode(callback, session)

        assert callback.answers[-1] == t("profile.setting_updated", "ru")
        assert "📦 Отправка: файлом" in message.edits[-1]
        assert '🛟 Поддержка: <a href="https://t.me/supbananify">@supbananify</a>' in message.edits[-1]
        assert "Потрачено" not in message.edits[-1]
        assert "Credits spent" not in message.edits[-1]
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[2][0].text == "🖼 Обычный формат"
        assert len(keyboard.inline_keyboard) == 3
        assert all("Назад" not in row[0].text for row in keyboard.inline_keyboard)

        from app.db import UserRepository

        assert await UserRepository(session).get_user_delivery_preference(602) is True


@pytest.mark.asyncio
async def test_show_profile_falls_back_to_english_when_language_code_missing(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState()
        message = FakeMessage(user_id=603)
        message.from_user.language_code = None

        await profile.show_profile(message, state, session)

        assert "💳 Balance: 5" in message.answers[-1]
        assert "🎨 Generations: 0" in message.answers[-1]
        assert '🛟 Support: <a href="https://t.me/supbananify">@supbananify</a>' in message.answers[-1]
        assert "🎁 Invited: 0" in message.answers[-1]
        assert "📦 Delivery: normal" in message.answers[-1]
        assert "Credits spent" not in message.answers[-1]
        assert "Потрачено" not in message.answers[-1]
        keyboard = message.answer_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == get_button_text("profile.top_up", "en")
        assert keyboard.inline_keyboard[1][0].text == get_button_text("profile.invite_friends", "en")
        assert keyboard.inline_keyboard[2][0].text == "📎 Send as file"


@pytest.mark.asyncio
async def test_referral_invite_screen_shows_referral_link_and_generates_missing_code(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "main_bot_username", "imai_test_bot")
    monkeypatch.setattr(profile.settings, "referral_enabled", True)
    async with session_factory() as session:
        session.add(User(id=604, balance=5, referral_code=None))
        await session.commit()
        message = FakeMessage(user_id=604)
        callback = FakeCallback(user_id=604, message=message, data="profile:invite_friends")

        await profile.show_referral_invite(callback, session)

        user = await session.get(User, 604)
        assert user is not None
        assert user.referral_code is not None
        assert user.start_payload is not None
        assert "👥 Реферальная программа" in message.edits[-1]
        assert "🎁 Приглашайте друзей и получайте 5 кредитов за каждого нового пользователя." in message.edits[-1]
        assert "🔗 Ваша ссылка:" in message.edits[-1]
        assert f"https://t.me/imai_test_bot?start={user.start_payload}" in message.edits[-1]
        assert f"ref_{user.referral_code}" not in message.edits[-1]
        assert message.edit_markups[-1].inline_keyboard[0][0].text == "⬅️ Назад"
        assert message.edit_markups[-1].inline_keyboard[0][0].callback_data == "profile:open"


def test_referral_text_is_localized_for_all_supported_locales() -> None:
    expected_descriptions = {
        "en": "🎁 Invite friends and get 5 credits for every new user.",
        "ru": "🎁 Приглашайте друзей и получайте 5 кредитов за каждого нового пользователя.",
        "es": "🎁 Invita amigos y recibe 5 créditos por cada nuevo usuario.",
        "pt": "🎁 Convide amigos e ganhe 5 créditos por cada novo usuário.",
        "fr": "🎁 Invitez des amis et recevez 5 crédits pour chaque nouvel utilisateur.",
        "de": "🎁 Lade Freunde ein und erhalte 5 Credits für jeden neuen Nutzer.",
        "ar": "🎁 ادعُ أصدقاءك واحصل على 5 أرصدة لكل مستخدم جديد.",
        "hi": "🎁 दोस्तों को आमंत्रित करें और हर नए उपयोगकर्ता पर 5 क्रेडिट पाएं।",
        "zh": "🎁 邀请好友，每位新用户可获得 5 积分。",
        "id": "🎁 Undang teman dan dapatkan 5 kredit untuk setiap pengguna baru.",
    }
    for lang in SUPPORTED_LANGUAGES:
        text = profile.build_referral_text("https://t.me/imai_test_bot?start=X7pQ2Lm9Ka", lang)

        assert t("profile.referral.title", lang) in text
        assert expected_descriptions[lang] in text
        assert t("profile.referral.link", lang) in text
        assert "https://t.me/imai_test_bot?start=X7pQ2Lm9Ka" in text
        assert "ref_" not in text


def test_support_contact_is_localized_for_all_supported_locales() -> None:
    expected_labels = {
        "en": "🛟 Support:",
        "ru": "🛟 Поддержка:",
        "es": "🛟 Soporte:",
        "pt": "🛟 Suporte:",
        "fr": "🛟 Support:",
        "de": "🛟 Support:",
        "ar": "🛟 الدعم:",
        "hi": "🛟 समर्थन:",
        "zh": "🛟 支持:",
        "id": "🛟 Dukungan:",
    }
    support_link = profile.build_support_link()

    assert support_link == '<a href="https://t.me/supbananify">@supbananify</a>'
    for lang in SUPPORTED_LANGUAGES:
        text = profile.build_support_contact_text(lang)

        assert expected_labels[lang] in text
        assert support_link in text
        assert "https://t.me/supbananify" in text


@pytest.mark.asyncio
async def test_referral_invite_button_hidden_when_referrals_disabled(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "referral_enabled", False)
    async with session_factory() as session:
        state = FakeState()
        message = FakeMessage(user_id=609)

        await profile.show_profile(message, state, session)

        button_texts = [button.text for row in message.answer_markups[-1].inline_keyboard for button in row]
        assert "🎁 Пригласить друзей" not in button_texts
        assert len(message.answer_markups[-1].inline_keyboard) == 2


@pytest.mark.asyncio
async def test_referral_invite_screen_hidden_when_referrals_disabled(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "referral_enabled", False)
    async with session_factory() as session:
        message = FakeMessage(user_id=610)
        callback = FakeCallback(user_id=610, message=message, data="profile:invite_friends")

        await profile.show_referral_invite(callback, session)

        assert message.edits == []
        assert callback.answers == [None]


@pytest.mark.asyncio
async def test_profile_invited_count_uses_accepted_referrals(session_factory) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                User(id=605, balance=5, referral_code="ref605"),
                User(id=606, balance=5, referral_code="ref606", referred_by_user_id=605),
                User(id=607, balance=5, referral_code="ref607", referred_by_user_id=605),
                User(id=608, balance=5, referral_code="ref608"),
            ]
        )
        await session.commit()
        session.add_all(
            [
                ReferralEvent(referrer_user_id=605, referred_user_id=606, referral_code="ref605", status=ReferralEventStatus.ACCEPTED.value),
                ReferralEvent(referrer_user_id=605, referred_user_id=607, referral_code="ref605", status=ReferralEventStatus.ACCEPTED.value),
                ReferralEvent(referrer_user_id=605, referred_user_id=608, referral_code="bad", status=ReferralEventStatus.REJECTED.value),
            ]
        )
        await session.commit()
        state = FakeState()
        message = FakeMessage(user_id=605)

        await profile.show_profile(message, state, session)

        assert "🎁 Приглашено: 2" in message.answers[-1]

        result = await session.execute(select(ReferralEvent).where(ReferralEvent.referrer_user_id == 605))
        assert len(result.scalars().all()) == 3
