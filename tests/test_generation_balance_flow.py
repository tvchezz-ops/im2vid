"""Tests for generation balance debit and refund flow."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.routers import generations
from app.db.base import Base
from app.db.models import GenerationRequest, GenerationRequestStatus, User
from app.db.repositories import GenerationRepository, UserRepository
from app.bot.states import GenerationStates
from app.utils import WavespeedFailedError, WavespeedTimeoutError


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
    def __init__(self, chat_id: int = 1):
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(
            id=chat_id,
            username="tester",
            first_name="Test",
            last_name=None,
            language_code="ru",
            is_bot=False,
            is_premium=False,
        )
        self.text = None
        self.photo = []
        self.document = None
        self.video = None
        self.voice = None
        self.audio = None
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.answer_markups: list[object] = []
        self.edit_markups: list[object] = []

    async def answer(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.answers.append(text)
        self.answer_markups.append(reply_markup)

    async def edit_text(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.edits.append(text)
        self.edit_markups.append(reply_markup)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        return None


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
        self.message = message or FakeMessage(chat_id=user_id)
        self.bot = object()
        self.data = data
        self.answered = False

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answered = True


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "generation-balance.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


async def create_user(session, user_id: int, balance: int) -> User:
    user = User(id=user_id, balance=balance)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_user_balance(session, user_id: int) -> int:
    result = await session.execute(select(User.balance).where(User.id == user_id))
    return int(result.scalar_one())


async def get_generation_status(session, generation_id) -> GenerationRequestStatus:
    result = await session.execute(
        select(GenerationRequest.status).where(GenerationRequest.id == generation_id)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_show_generation_menu_starts_with_generation_type_selection() -> None:
    message = FakeMessage(chat_id=401)
    state = FakeState()
    message.from_user = SimpleNamespace(id=401)

    await generations.show_generation_menu(message, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert "Выберите тип генерации:" in message.answers[-1]
    assert "Lipsync (озвучка лица)" in message.answers[-1]
    assert "Анимация лица под аудио или текст" in message.answers[-1]
    keyboard = message.answer_markups[-1]
    callback_data = [row[0].callback_data for row in keyboard.inline_keyboard[:-1]]
    assert "gen:section:lipsync" in callback_data


@pytest.mark.asyncio
async def test_choose_generation_type_shows_models_for_type() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=402)
    callback = FakeCallback(user_id=402, message=message, data="gen:type:image_edit")

    await generations.choose_generation_type(callback, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert state.data["selected_generation_type"] == "image_edit"
    assert state.data["selected_provider"] is None
    assert message.edits[-1] == "Выберите модель:"


@pytest.mark.asyncio
async def test_choose_all_models_shows_provider_list() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=403)
    callback = FakeCallback(user_id=403, message=message, data="gen:type:all")

    await generations.choose_generation_type(callback, state)

    assert state.state == GenerationStates.choosing_provider
    assert state.data["selected_generation_type"] == "all"
    assert state.data["selected_provider"] is None
    assert message.edits[-1] == "Выберите провайдера:"


@pytest.mark.asyncio
async def test_choose_provider_shows_provider_models() -> None:
    state = FakeState({"selected_generation_type": "all"})
    message = FakeMessage(chat_id=404)
    callback = FakeCallback(user_id=404, message=message, data="gen:provider:google")

    await generations.choose_provider(callback, state)

    assert state.state == GenerationStates.choosing_provider
    assert state.data["selected_provider"] == "google"
    assert message.edits[-1] == "Выберите модель:"


@pytest.mark.asyncio
async def test_choose_provider_shows_midjourney_placeholder_when_models_absent() -> None:
    state = FakeState({"selected_generation_type": "all"})
    message = FakeMessage(chat_id=404)
    callback = FakeCallback(user_id=404, message=message, data="gen:provider:midjourney")

    await generations.choose_provider(callback, state)

    assert state.state == GenerationStates.choosing_provider
    assert state.data["selected_provider"] is None
    assert message.edits[-1] == "У этого провайдера пока нет подключённых моделей"


@pytest.mark.asyncio
async def test_back_to_generation_models_returns_to_provider_filtered_models() -> None:
    state = FakeState(
        {
            "selected_generation_type": "all",
            "selected_provider": "google",
            "model_key": "nano_banana",
            "model_title": "Nano Banana",
            "model_endpoint": "/api/v3/nano-banana",
            "user_settings": {"aspect_ratio": "1:1"},
            "current_setting_key": "aspect_ratio",
            "input_image_file_id": "file-id",
            "prompt": "Prompt text",
        }
    )
    message = FakeMessage(chat_id=405)
    callback = FakeCallback(user_id=405, message=message, data="gen:back_models")

    await generations.back_to_generation_models(callback, state)

    assert state.state == GenerationStates.choosing_provider
    assert message.edits[-1] == "Выберите модель:"
    assert state.data["model_key"] is None
    assert state.data["prompt"] is None


@pytest.mark.asyncio
async def test_back_to_generation_types_from_provider_screen() -> None:
    state = FakeState({"selected_generation_type": "all", "selected_provider": "google"})
    message = FakeMessage(chat_id=406)
    callback = FakeCallback(user_id=406, message=message, data="gen:back_types")

    await generations.back_to_generation_types(callback, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert state.data["selected_generation_type"] is None
    assert state.data["selected_provider"] is None
    assert "Выберите тип генерации:" in message.edits[-1]
    assert "Lipsync (озвучка лица)" in message.edits[-1]


@pytest.mark.asyncio
async def test_continue_after_settings_shows_lipsync_media_prompt() -> None:
    state = FakeState({"model_generation_type": "lipsync"})
    message = FakeMessage(chat_id=407)
    callback = FakeCallback(user_id=407, message=message, data="gen:continue")

    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_image
    assert "Вы выбрали Lipsync." in message.edits[-1]
    assert "Отправьте фото или видео, затем текст или голос для озвучки." in message.edits[-1]


@pytest.mark.asyncio
async def test_send_confirmation_screen_shows_lipsync_incomplete_error(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState({"model_key": "nano_banana", "model_generation_type": "lipsync"})
        message = FakeMessage(chat_id=411)

        await generations.send_confirmation_screen(
            message=message,
            state=state,
            session=session,
            telegram_user=message.from_user,
            edit=False,
        )

        assert message.answers[-1] == "❌ Для lipsync нужно изображение/видео и текст или аудио."


@pytest.mark.asyncio
async def test_process_generation_image_saves_lipsync_video_as_input_media() -> None:
    state = FakeState({"model_generation_type": "lipsync"})
    message = FakeMessage(chat_id=408)
    message.video = SimpleNamespace(file_id="video-file-id")

    await generations.process_generation_image(message, state)

    assert state.state == GenerationStates.waiting_for_prompt
    assert state.data["input_media"] == {"type": "video", "file_id": "video-file-id"}
    assert state.data["input_image_file_id"] is None
    assert "текст или голосовое сообщение" in message.answers[-1]


@pytest.mark.asyncio
async def test_process_prompt_saves_lipsync_text_input(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=409, balance=2)
        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_generation_type": "lipsync",
                "model_title": "Lip Model",
                "input_media": {"type": "photo", "file_id": "photo-file-id"},
                "user_settings": {},
            }
        )
        message = FakeMessage(chat_id=409)
        message.text = "Озвучь это спокойным голосом"

        await generations.process_prompt(message, state, session)

        assert state.data["input_audio_or_text"] == {
            "type": "text",
            "text": "Озвучь это спокойным голосом",
        }
        assert state.data["prompt"] == "Озвучь это спокойным голосом"


@pytest.mark.asyncio
async def test_process_prompt_saves_lipsync_voice_input(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=410, balance=2)
        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_generation_type": "lipsync",
                "model_title": "Lip Model",
                "input_media": {"type": "photo", "file_id": "photo-file-id"},
                "user_settings": {},
            }
        )
        message = FakeMessage(chat_id=410)
        message.voice = SimpleNamespace(file_id="voice-file-id")

        await generations.process_prompt(message, state, session)

        assert state.data["input_audio_or_text"] == {
            "type": "voice",
            "file_id": "voice-file-id",
        }
        assert state.data["prompt"] == "Голосовое сообщение"


@pytest.mark.asyncio
async def test_confirm_generation_debits_one_credit(session_factory, monkeypatch, tmp_path) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=101, balance=3)

        temp_input_path = tmp_path / "input.png"
        temp_input_path.write_bytes(b"input")

        class FakeTelegramFilesService:
            def __init__(self, bot):
                self.bot = bot

            async def download_temp_file_and_get_public_url(self, file_id: str):
                return SimpleNamespace(local_path=temp_input_path, public_url="https://example.com/input.png")

        async def fake_poll_generation_result(**kwargs) -> None:
            return None

        monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)
        monkeypatch.setattr(generations, "poll_generation_result", fake_poll_generation_result)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Make the image brighter and keep the subject intact",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png"},
            }
        )
        callback = FakeCallback(user_id=101)

        try:
            await generations.confirm_generation(callback, state, session)
            await generations.ACTIVE_GENERATIONS[101]["task"]
            assert await get_user_balance(session, 101) == 2
        finally:
            generations.ACTIVE_GENERATIONS.clear()
            Path(temp_input_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_failed_generation_refunds_credit(session_factory, monkeypatch, tmp_path) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=201, balance=4)
        generation = await GenerationRepository(session).create_generation_request(
            user_id=201,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt",
            settings={},
            status="created",
            cost=1,
        )

    temp_input_path = tmp_path / "failed-input.png"
    temp_input_path.write_bytes(b"input")

    class FailedWavespeedService:
        async def submit_generation(self, model_key: str, payload: dict[str, object]):
            return SimpleNamespace(prediction_id="pred-1")

        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60):
            raise WavespeedFailedError("failed")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(generations, "WavespeedService", FailedWavespeedService)

    bot = SimpleNamespace(send_message=_async_collector(), send_photo=_async_collector(), send_document=_async_collector())
    state = FakeState()

    await generations.poll_generation_result(
        bot=bot,
        state=state,
        user_id=201,
        chat_id=201,
        generation_request_id=generation.id,
        model_key="nano_banana",
        cost=1,
        payload={"prompt": "Prompt"},
        temp_input_path=str(temp_input_path),
    )

    async with session_maker() as session:
        assert await get_user_balance(session, 201) == 5
        assert await get_generation_status(session, generation.id) == GenerationRequestStatus.FAILED


@pytest.mark.asyncio
async def test_timeout_generation_refunds_credit(session_factory, monkeypatch, tmp_path) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=301, balance=4)
        generation = await GenerationRepository(session).create_generation_request(
            user_id=301,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt",
            settings={},
            status="created",
            cost=1,
        )

    temp_input_path = tmp_path / "timeout-input.png"
    temp_input_path.write_bytes(b"input")

    class TimeoutWavespeedService:
        async def submit_generation(self, model_key: str, payload: dict[str, object]):
            return SimpleNamespace(prediction_id="pred-2")

        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60):
            raise WavespeedTimeoutError("timeout")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(generations, "WavespeedService", TimeoutWavespeedService)

    bot = SimpleNamespace(send_message=_async_collector(), send_photo=_async_collector(), send_document=_async_collector())
    state = FakeState()

    await generations.poll_generation_result(
        bot=bot,
        state=state,
        user_id=301,
        chat_id=301,
        generation_request_id=generation.id,
        model_key="nano_banana",
        cost=1,
        payload={"prompt": "Prompt"},
        temp_input_path=str(temp_input_path),
    )

    async with session_maker() as session:
        assert await get_user_balance(session, 301) == 5
        assert await get_generation_status(session, generation.id) == GenerationRequestStatus.TIMEOUT


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_start_submit(session_factory, monkeypatch) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=401, balance=0)

        called = False

        async def fake_poll_generation_result(**kwargs) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(generations, "poll_generation_result", fake_poll_generation_result)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Make the image brighter and keep the subject intact",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png"},
            }
        )
        callback = FakeCallback(user_id=401)

        await generations.confirm_generation(callback, state, session)

        assert called is False
        assert callback.message.answers[-1] == "Недостаточно кредитов. Пополните баланс в магазине."
        assert await get_user_balance(session, 401) == 0


def _async_collector():
    async def _call(*args, **kwargs):
        return None

    return _call