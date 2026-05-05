from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.routers import profile
from app.db.base import Base


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
    def __init__(self, user_id: int = 1, message: FakeMessage | None = None):
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
        self.data = "profile:toggle_delivery_mode"
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append(text)


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
async def test_show_profile_displays_delivery_mode_and_toggle_button(session_factory) -> None:
    async with session_factory() as session:
        message = FakeMessage(user_id=601)

        await profile.show_profile(message, session)

        assert "Способ отправки: Обычный формат" in message.answers[-1]
        keyboard = message.answer_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "📎 Отправлять файлом"


@pytest.mark.asyncio
async def test_toggle_delivery_mode_updates_profile_message(session_factory) -> None:
    async with session_factory() as session:
        message = FakeMessage(user_id=602)
        callback = FakeCallback(user_id=602, message=message)

        await profile.toggle_delivery_mode(callback, session)

        assert callback.answers[-1] == "Настройка обновлена"
        assert "Способ отправки: Файлом без сжатия" in message.edits[-1]
        keyboard = message.edit_markups[-1]
        assert keyboard.inline_keyboard[0][0].text == "🖼 Отправлять обычным форматом"