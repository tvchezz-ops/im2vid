"""Tests for generation balance debit and refund flow."""

from __future__ import annotations

import logging
import os
import asyncio
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
from app.bot.keyboards import build_main_menu_keyboard, get_button_text
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
        self.bot = object()
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


class FakeBot:
    def __init__(self):
        self.documents: list[dict[str, object]] = []
        self.photos: list[dict[str, object]] = []
        self.videos: list[dict[str, object]] = []
        self.messages: list[str] = []
        self.message_markups: list[object] = []

    async def send_document(self, chat_id, document, caption=None, reply_markup=None, request_timeout=None):
        self.documents.append(
            {
                "chat_id": chat_id,
                "document": document,
                "caption": caption,
                "reply_markup": reply_markup,
                "request_timeout": request_timeout,
            }
        )

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.photos.append(
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "reply_markup": reply_markup,
            }
        )

    async def send_video(self, chat_id, video, caption=None, reply_markup=None, request_timeout=None):
        self.videos.append(
            {
                "chat_id": chat_id,
                "video": video,
                "caption": caption,
                "reply_markup": reply_markup,
                "request_timeout": request_timeout,
            }
        )

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append(text)
        self.message_markups.append(reply_markup)


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


async def get_user_delivery_preference(session, user_id: int) -> bool:
    result = await session.execute(select(User.send_results_as_files).where(User.id == user_id))
    value = result.scalar_one()
    return bool(value)


async def get_user_balance(session, user_id: int) -> int:
    result = await session.execute(select(User.balance).where(User.id == user_id))
    return int(result.scalar_one())


async def get_generation_status(session, generation_id) -> GenerationRequestStatus:
    result = await session.execute(
        select(GenerationRequest.status).where(GenerationRequest.id == generation_id)
    )
    return result.scalar_one()


async def await_background_generation_tasks() -> None:
    current_loop = asyncio.get_running_loop()
    tasks = {
        entry["task"]
        for entry in generations.BACKGROUND_GENERATIONS.values()
        if entry["task"].get_loop() is current_loop
    }
    if tasks:
        await asyncio.gather(*tasks)
    generations.BACKGROUND_GENERATIONS.clear()


@pytest.mark.asyncio
async def test_show_generation_menu_starts_with_generation_type_selection() -> None:
    message = FakeMessage(chat_id=401)
    state = FakeState()
    message.from_user = SimpleNamespace(id=401)

    await generations.show_generation_menu(message, state, None)

    assert state.state == GenerationStates.choosing_generation_type
    assert "Choose generation type:" in message.answers[-1]
    assert "Text to Video" in message.answers[-1]
    keyboard = message.answer_markups[-1]
    callback_data = [row[0].callback_data for row in keyboard.inline_keyboard[:-1]]
    assert "gen:section:lipsync" not in callback_data


@pytest.mark.asyncio
@pytest.mark.parametrize("active_count", [0, 1, 2])
async def test_show_generation_menu_allows_under_parallel_limit(session_factory, active_count: int) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=421, balance=5)
        for index in range(active_count):
            await GenerationRepository(session).create_generation_request(
                user_id=421,
                model_key="nano_banana",
                model_endpoint="/api/v3/nano-banana",
                prompt=f"Prompt {index}",
                settings={},
                status="created",
                cost=1,
            )

        message = FakeMessage(chat_id=421)
        state = FakeState()

        await generations.show_generation_menu(message, state, session)

        assert state.state == GenerationStates.choosing_generation_type
        assert "Выберите тип генерации:" in message.answers[-1]


@pytest.mark.asyncio
async def test_show_generation_menu_blocks_at_parallel_limit(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=422, balance=5)
        for index, status in enumerate(("created", "processing", "pending")):
            await GenerationRepository(session).create_generation_request(
                user_id=422,
                model_key="nano_banana",
                model_endpoint="/api/v3/nano-banana",
                prompt=f"Prompt {index}",
                settings={},
                status=status,
                cost=1,
            )

        message = FakeMessage(chat_id=422)
        state = FakeState()

        await generations.show_generation_menu(message, state, session)

        assert state.state is None
        assert message.answers[-1] == "⚠️ У вас уже запущено 3 генерации. Дождитесь завершения одной из них."


@pytest.mark.asyncio
async def test_count_active_generations_uses_only_active_statuses(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=423, balance=5)
        repository = GenerationRepository(session)
        for status in ("created", "processing", "pending", "completed", "failed", "timeout", "cancelled"):
            await repository.create_generation_request(
                user_id=423,
                model_key="nano_banana",
                model_endpoint="/api/v3/nano-banana",
                prompt=status,
                settings={},
                status=status,
                cost=1,
            )

        assert await repository.count_active_generations(423) == 3


@pytest.mark.asyncio
async def test_choose_generation_section_shows_models_for_type() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=402)
    callback = FakeCallback(user_id=402, message=message, data="gen:section:image_edit")

    await generations.choose_generation_section(callback, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert state.data["selected_generation_type"] == "image_edit"
    assert state.data["selected_provider"] is None
    assert message.edits[-1] == "Выберите модель:"


@pytest.mark.asyncio
async def test_choose_all_models_shows_provider_list() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=403)
    callback = FakeCallback(user_id=403, message=message, data="gen:all")

    await generations.show_all_generation_providers(callback, state)

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
    callback = FakeCallback(user_id=405, message=message, data="gen:back:models")

    await generations.back_to_generation_models(callback, state)

    assert state.state == GenerationStates.choosing_provider
    assert message.edits[-1] == "Выберите модель:"
    assert state.data["model_key"] is None
    assert state.data["prompt"] is None


@pytest.mark.asyncio
async def test_back_to_generation_types_from_provider_screen() -> None:
    state = FakeState({"selected_generation_type": "all", "selected_provider": "google"})
    message = FakeMessage(chat_id=406)
    callback = FakeCallback(user_id=406, message=message, data="gen:back:sections")

    await generations.back_to_generation_sections(callback, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert state.data["selected_generation_type"] is None
    assert state.data["selected_provider"] is None
    assert "Выберите тип генерации:" in message.edits[-1]
    assert "Text → Video" in message.edits[-1]


@pytest.mark.asyncio
async def test_unknown_generation_callback_shows_fallback_alert_screen() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=406)
    callback = FakeCallback(user_id=406, message=message, data="gen:obsolete")

    await generations.handle_unknown_generation_callback(callback, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert "Выберите тип генерации:" in message.edits[-1]


@pytest.mark.asyncio
async def test_open_setting_selector_and_choose_setting_value_for_model_with_settings() -> None:
    state = FakeState(
        {
            "model_key": "nano_banana",
            "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png"},
        }
    )
    message = FakeMessage(chat_id=450)
    open_callback = FakeCallback(user_id=450, message=message, data="gen:setting:aspect_ratio")

    await generations.open_setting_selector(open_callback, state)

    assert state.data["current_setting_key"] == "aspect_ratio"
    assert "Параметр: <b>Формат</b>" in message.edits[-1]

    choose_callback = FakeCallback(user_id=450, message=message, data="gen:set:aspect_ratio:8")
    await generations.choose_setting_value(choose_callback, state)

    assert state.data["user_settings"]["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_open_setting_selector_for_text_setting_switches_to_text_input() -> None:
    state = FakeState(
        {
            "model_key": "alibaba_wan_2_6_text_to_video",
            "user_settings": {"size": "1280*720", "negative_prompt": ""},
        }
    )
    message = FakeMessage(chat_id=451)
    callback = FakeCallback(user_id=451, message=message, data="gen:setting:negative_prompt")

    await generations.open_setting_selector(callback, state)

    assert state.state == GenerationStates.waiting_for_setting_text
    assert state.data["current_setting_key"] == "negative_prompt"
    assert "Параметр: <b>Negative prompt</b>" in message.edits[-1]
    assert "Что нужно исключить из результата" in message.edits[-1]


@pytest.mark.asyncio
async def test_process_text_setting_value_saves_text_and_returns_to_settings() -> None:
    state = FakeState(
        {
            "model_key": "alibaba_wan_2_6_text_to_video",
            "current_setting_key": "negative_prompt",
            "user_settings": {"size": "1280*720", "negative_prompt": ""},
        }
    )
    state.state = GenerationStates.waiting_for_setting_text
    message = FakeMessage(chat_id=452)
    message.text = "blur, noise"

    await generations.process_text_setting_value(message, state)

    assert state.state == GenerationStates.choosing_settings
    assert state.data["user_settings"]["negative_prompt"] == "blur, noise"
    assert message.answers[0] == "Значение сохранено."


@pytest.mark.asyncio
async def test_process_text_setting_value_clears_dash_to_empty_string() -> None:
    state = FakeState(
        {
            "model_key": "alibaba_wan_2_6_text_to_video",
            "current_setting_key": "negative_prompt",
            "user_settings": {"size": "1280*720", "negative_prompt": "existing"},
        }
    )
    state.state = GenerationStates.waiting_for_setting_text
    message = FakeMessage(chat_id=453)
    message.text = "-"

    await generations.process_text_setting_value(message, state)

    assert state.state == GenerationStates.choosing_settings
    assert state.data["user_settings"]["negative_prompt"] == ""


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
async def test_continue_after_settings_for_text_to_image_goes_to_prompt() -> None:
    state = FakeState({"model_key": "alibaba_wan_2_6_text_to_image", "model_generation_type": "text_to_image"})
    message = FakeMessage(chat_id=470)
    callback = FakeCallback(user_id=470, message=message, data="gen:continue")

    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_prompt
    assert message.edits[-1] == "Опишите изображение, которое хотите создать."

@pytest.mark.asyncio
async def test_continue_after_settings_for_video_edit_goes_to_video_step() -> None:
    state = FakeState({"model_key": "google_veo3_1_fast_video_extend", "model_generation_type": "video_edit"})
    message = FakeMessage(chat_id=471)
    callback = FakeCallback(user_id=471, message=message, data="gen:continue")

    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_video
    assert message.edits[-1] == "Отправьте видео для модели Google Veo3.1 Fast Video Extend."


@pytest.mark.asyncio
async def test_continue_after_settings_for_multi_image_model_goes_to_images_step() -> None:
    state = FakeState({"model_key": "nano_banana", "model_generation_type": "image_edit"})
    message = FakeMessage(chat_id=476)
    callback = FakeCallback(user_id=476, message=message, data="gen:continue")

    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_images
    assert message.edits[-1] == (
        "Отправьте изображения для модели Google Nano Banana Pro Edit Ultra.\n"
        "Можно загрузить от 1 до 14 файлов.\n"
        "После загрузки нажмите ✅ Продолжить."
    )


@pytest.mark.asyncio
async def test_continue_after_multi_image_upload_requires_minimum_images() -> None:
    state = FakeState(
        {
            "model_key": "nano_banana",
            "model_generation_type": "image_edit",
            "input_media_items": [],
        }
    )
    message = FakeMessage(chat_id=477)

    await generations.continue_after_multi_image_upload(message, state)

    assert state.state is None
    assert message.answers[-1] == "❌ Ошибка E003: нужно загрузить минимум 1 изображение."


@pytest.mark.asyncio
async def test_process_generation_images_appends_uploaded_media(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        return {
            "type": "photo",
            "file_id": "photo-file-id",
            "local_path": "/tmp/photo-file-id.png",
            "public_url": "https://example.com/photo-file-id.png",
        }

    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)

    state = FakeState({"model_key": "nano_banana", "model_generation_type": "image_edit", "input_media_items": []})
    message = FakeMessage(chat_id=478)
    message.photo = [SimpleNamespace(file_id="photo-file-id")]

    await generations.process_generation_images(message, state)

    assert state.data["input_media"] == {"type": "images", "count": 1}
    assert state.data["input_image_file_id"] == "photo-file-id"
    assert state.data["input_media_items"][0]["public_url"] == "https://example.com/photo-file-id.png"
    assert "Загружено 1 из 14." in message.answers[-1]


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
async def test_send_confirmation_screen_falls_back_to_english_language(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_generation_type": "image_edit",
                "prompt": "Make the image brighter and keep the subject intact",
                "input_media": {"type": "image", "file_id": "telegram-file-id"},
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "1"},
            }
        )
        message = FakeMessage(chat_id=412)
        message.from_user.language_code = None

        await generations.send_confirmation_screen(
            message=message,
            state=state,
            session=session,
            telegram_user=message.from_user,
            edit=False,
        )

        assert state.data["user_language"] == "en"
        assert message.answer_markups[-1].inline_keyboard[0][0].text == get_button_text("generation.confirm", "en")


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
async def test_process_generation_image_rejects_video_for_image_flow() -> None:
    state = FakeState({"model_generation_type": "image_edit"})
    message = FakeMessage(chat_id=472)
    message.video = SimpleNamespace(file_id="video-file-id")

    await generations.process_generation_image(message, state)

    assert message.answers[-1] == "❌ Ошибка E001: Нужно отправить изображение, не видео."

@pytest.mark.asyncio
async def test_process_generation_video_accepts_video_document(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        return {
            "type": "video",
            "file_id": "video-doc-id",
            "local_path": "/tmp/video-doc-id.mp4",
            "public_url": "https://example.com/video-doc-id.mp4",
        }

    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)

    state = FakeState({"model_generation_type": "video_edit"})
    message = FakeMessage(chat_id=473)
    message.document = SimpleNamespace(file_id="video-doc-id", mime_type="video/mp4")

    await generations.process_generation_video(message, state)

    assert state.state == GenerationStates.waiting_for_prompt
    assert state.data["input_media"] == {"type": "video", "count": 1}
    assert state.data["input_media_items"][0]["public_url"] == "https://example.com/video-doc-id.mp4"

@pytest.mark.asyncio
async def test_process_generation_video_rejects_photo_for_video_flow() -> None:
    state = FakeState({"model_generation_type": "video_edit"})
    message = FakeMessage(chat_id=474)
    message.photo = [SimpleNamespace(file_id="photo-file-id")]

    await generations.process_generation_video(message, state)

    assert message.answers[-1] == "❌ Ошибка E001: Нужно отправить видео, не изображение."

@pytest.mark.asyncio
async def test_process_prompt_rejects_file_when_text_prompt_expected(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=475, balance=2)
        state = FakeState(
            {
                "model_key": "alibaba_wan_2_6_text_to_image",
                "model_generation_type": "text_to_image",
                "model_title": "Alibaba Wan 2.6 Text To Image",
                "user_settings": {},
            }
        )
        message = FakeMessage(chat_id=475)
        message.photo = [SimpleNamespace(file_id="photo-file-id")]

        await generations.process_prompt(message, state, session)

        assert message.answers[-1] == "❌ Ошибка E001: на этом этапе нужен только текстовый prompt."


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
async def test_process_prompt_insufficient_balance_returns_to_settings(session_factory, monkeypatch, caplog) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=411, balance=0)
        confirmation_opened = False

        async def fake_send_confirmation_screen(**kwargs) -> None:
            nonlocal confirmation_opened
            confirmation_opened = True

        monkeypatch.setattr(generations, "send_confirmation_screen", fake_send_confirmation_screen)

        state = FakeState(
            {
                "model_key": "alibaba_wan_2_6_text_to_image",
                "model_generation_type": "text_to_image",
                "model_title": "Alibaba Wan 2.6 Text To Image",
                "user_settings": {
                    "num_generations": "4",
                },
            }
        )
        state.state = GenerationStates.waiting_for_prompt
        message = FakeMessage(chat_id=411)
        message.text = "Generate four variants with dramatic lighting"

        with caplog.at_level(logging.INFO):
            await generations.process_prompt(message, state, session)

        assert confirmation_opened is False
        assert state.state == GenerationStates.choosing_settings
        assert state.data.get("prompt") is None
        assert message.answers[0] == "❌ Ошибка E006: недостаточно кредитов. Нужно 4, у вас 0."
        assert message.answers[1] == "Измените количество генераций или пополните баланс."
        assert message.answer_markups[1].keyboard[0][0].text == "🎨 Генерации"
        assert message.answers[2].startswith("Настройки модели:")
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "insufficient_balance"
            and record.msg.get("user_id") == 411
            and record.msg.get("state") == GenerationStates.waiting_for_prompt.state
            and record.msg.get("model_key") == "alibaba_wan_2_6_text_to_image"
            and record.msg.get("total_cost") == 4
            for record in caplog.records
        )


@pytest.mark.asyncio
async def test_insufficient_balance_exits_waiting_for_prompt_so_back_text_cannot_retrigger_e006(session_factory, monkeypatch) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=412, balance=0)
        logged_codes: list[str] = []
        original_log_generation_error = generations.log_generation_error

        def fake_log_generation_error(code: str, **kwargs) -> None:
            logged_codes.append(code)
            original_log_generation_error(code, **kwargs)

        monkeypatch.setattr(generations, "log_generation_error", fake_log_generation_error)

        state = FakeState(
            {
                "model_key": "alibaba_wan_2_6_text_to_image",
                "model_generation_type": "text_to_image",
                "model_title": "Alibaba Wan 2.6 Text To Image",
                "user_settings": {
                    "num_generations": "4",
                },
            }
        )
        state.state = GenerationStates.waiting_for_prompt
        message = FakeMessage(chat_id=412)
        message.text = "Generate four variants with dramatic lighting"

        await generations.process_prompt(message, state, session)

        assert state.state == GenerationStates.choosing_settings
        assert logged_codes.count(generations.ErrorCode.E006_INSUFFICIENT_BALANCE) == 1
        assert state.state != GenerationStates.waiting_for_prompt


@pytest.mark.asyncio
async def test_back_to_settings_from_waiting_for_prompt_restores_settings_and_logs(caplog) -> None:
    state = FakeState(
        {
            "model_key": "alibaba_wan_2_6_text_to_image",
            "model_generation_type": "text_to_image",
            "model_title": "Alibaba Wan 2.6 Text To Image",
            "user_settings": {},
            "input_media": {"type": "image", "count": 1},
            "input_media_items": [],
            "input_image_file_id": "photo-file-id",
            "input_media_urls": ["https://example.com/input.png"],
            "input_media_paths": ["/tmp/input.png"],
            "prompt": "old prompt",
        }
    )
    state.state = GenerationStates.waiting_for_prompt
    message = FakeMessage(chat_id=476)
    message.text = "⬅️ Назад к настройкам"

    with caplog.at_level(logging.INFO):
        await generations.back_to_settings_from_input_step(message, state)

    assert state.state == GenerationStates.choosing_settings
    assert state.data["input_media"] is None
    assert state.data["input_media_items"] == []
    assert state.data["input_image_file_id"] is None
    assert state.data["input_media_urls"] == []
    assert state.data["input_media_paths"] == []
    assert state.data["prompt"] is None
    assert state.data["selected_model_key"] == "alibaba_wan_2_6_text_to_image"
    assert state.data["selected_settings"] == {}
    assert message.answer_markups[0].keyboard[0][0].text == "🎨 Генерации"
    assert message.answers[0] == "Возвращаю к настройкам модели."
    assert message.answers[1].startswith("Настройки модели:")
    assert any(
        isinstance(record.msg, dict)
        and record.msg.get("action") == "back_to_settings"
        and record.msg.get("user_id") == 476
        and record.msg.get("state") == GenerationStates.waiting_for_prompt.state
        and record.msg.get("model_key") == "alibaba_wan_2_6_text_to_image"
        and record.msg.get("incoming_text_type") == "text"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_back_to_settings_from_waiting_for_prompt_without_model_shows_sections() -> None:
    state = FakeState(
        {
            "user_settings": {},
            "input_media": {"type": "image", "count": 1},
            "input_media_items": [],
            "input_image_file_id": "photo-file-id",
        }
    )
    state.state = GenerationStates.waiting_for_prompt
    message = FakeMessage(chat_id=477)
    message.text = "⬅️ Назад к настройкам"

    await generations.back_to_settings_from_input_step(message, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert state.data == {
        "selected_generation_type": None,
        "selected_provider": None,
    }
    assert message.answer_markups[0].keyboard[0][0].text == "🎨 Генерации"
    assert message.answers[0] == "Возвращаю к выбору разделов генерации."
    assert "Выберите тип генерации:" in message.answers[1]


@pytest.mark.asyncio
async def test_send_generation_outputs_keeps_main_menu_keyboard_without_menu_message(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "output.jpg"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/jpeg", 5

    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)

    bot = FakeBot()
    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.jpg"])

    assert delivered.delivered_successfully is True
    assert delivered.success is True
    assert delivered.method == "photo"
    assert delivered.error_code is None
    assert "🏠 Главное меню" not in bot.messages
    assert bot.photos[-1]["reply_markup"].keyboard[0][0].text == get_button_text("main.generations", "en")


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

        async def fake_submit_generation_request(**kwargs) -> str:
            return "pred-101"

        monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)
        monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)
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
            await await_background_generation_tasks()
            assert await get_user_balance(session, 101) == 2
        finally:
            generations.BACKGROUND_GENERATIONS.clear()
            Path(temp_input_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_confirm_generation_reuses_uploaded_multi_image_items(session_factory, monkeypatch, tmp_path) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=102, balance=3)

        first_path = tmp_path / "input-1.png"
        second_path = tmp_path / "input-2.png"
        first_path.write_bytes(b"input-1")
        second_path.write_bytes(b"input-2")
        captured: dict[str, object] = {}

        async def fake_poll_generation_result(**kwargs) -> None:
            captured.update(kwargs)

        async def fake_submit_generation_request(**kwargs) -> str:
            captured["submit_payload"] = kwargs["payload"]
            return "pred-102"

        class ForbiddenTelegramFilesService:
            def __init__(self, bot):
                raise AssertionError("TelegramFilesService should not be called when input_media_items already exist")

        monkeypatch.setattr(generations, "TelegramFilesService", ForbiddenTelegramFilesService)
        monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)
        monkeypatch.setattr(generations, "poll_generation_result", fake_poll_generation_result)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Blend both inputs",
                "input_media": {"type": "images", "count": 2},
                "input_media_items": [
                    {
                        "type": "photo",
                        "file_id": "file-1",
                        "local_path": str(first_path),
                        "public_url": "https://example.com/input-1.png",
                    },
                    {
                        "type": "photo",
                        "file_id": "file-2",
                        "local_path": str(second_path),
                        "public_url": "https://example.com/input-2.png",
                    },
                ],
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png"},
            }
        )
        callback = FakeCallback(user_id=102)

        try:
            await generations.confirm_generation(callback, state, session)
            await await_background_generation_tasks()

            assert captured["submit_payload"] == {
                "images": [
                    "https://example.com/input-1.png",
                    "https://example.com/input-2.png",
                ],
                "prompt": "Blend both inputs",
                "aspect_ratio": "1:1",
                "resolution": "4k",
                "output_format": "png",
                "enable_sync_mode": False,
                "enable_base64_output": False,
            }
            assert captured["prediction_id"] == "pred-102"
            assert captured["temp_input_path"] == [str(first_path), str(second_path)]
            assert await get_user_balance(session, 102) == 2
        finally:
            generations.BACKGROUND_GENERATIONS.clear()
            first_path.unlink(missing_ok=True)
            second_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_confirm_generation_keeps_temp_media_until_polling_finishes(session_factory, monkeypatch, tmp_path) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=103, balance=3)

        temp_input_path = tmp_path / "input-persist.png"
        temp_input_path.write_bytes(b"input")
        captured: dict[str, object] = {}

        class FakeTelegramFilesService:
            def __init__(self, bot):
                self.bot = bot

            async def download_temp_file_and_get_public_url(self, file_id: str):
                return SimpleNamespace(
                    local_path=temp_input_path,
                    public_url="https://example.com/media/input-persist.png",
                )

        async def fake_poll_generation_result(**kwargs) -> None:
            captured["exists_during_poll"] = temp_input_path.exists()
            captured.update(kwargs)
            temp_input_path.unlink(missing_ok=True)

        async def fake_submit_generation_request(**kwargs) -> str:
            return "pred-103"

        monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)
        monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)
        monkeypatch.setattr(generations, "poll_generation_result", fake_poll_generation_result)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Keep the source available until background polling starts",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png"},
            }
        )
        callback = FakeCallback(user_id=103)

        try:
            await generations.confirm_generation(callback, state, session)
            assert state.state is None
            assert state.data == {}
            await await_background_generation_tasks()

            assert captured["exists_during_poll"] is True
            assert captured["temp_input_path"] == str(temp_input_path)
        finally:
            generations.BACKGROUND_GENERATIONS.clear()
            temp_input_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_confirm_generation_allows_new_flow_while_background_task_runs(session_factory, monkeypatch, tmp_path) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=104, balance=3)

        temp_input_path = tmp_path / "input-background.png"
        temp_input_path.write_bytes(b"input")
        release_polling = asyncio.Event()

        class FakeTelegramFilesService:
            def __init__(self, bot):
                self.bot = bot

            async def download_temp_file_and_get_public_url(self, file_id: str):
                return SimpleNamespace(
                    local_path=temp_input_path,
                    public_url="https://example.com/media/input-background.png",
                )

        async def fake_poll_generation_result(**kwargs) -> None:
            await release_polling.wait()

        async def fake_submit_generation_request(**kwargs) -> str:
            return "pred-104"

        monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)
        monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)
        monkeypatch.setattr(generations, "poll_generation_result", fake_poll_generation_result)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Start one generation and immediately open another flow",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png"},
            }
        )
        callback = FakeCallback(user_id=104)

        try:
            await generations.confirm_generation(callback, state, session)

            assert state.state is None
            assert state.data == {}
            assert temp_input_path.exists() is True

            message = FakeMessage(chat_id=104)
            await generations.show_generation_menu(message, state, session)

            assert state.state == GenerationStates.choosing_generation_type
            assert "Выберите тип генерации:" in message.answers[-1]
        finally:
            release_polling.set()
            await await_background_generation_tasks()
            generations.BACKGROUND_GENERATIONS.clear()
            temp_input_path.unlink(missing_ok=True)


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
            settings={"num_generations": "1"},
            status="created",
            cost=1,
        )

    temp_input_path = tmp_path / "failed-input.png"
    temp_input_path.write_bytes(b"input")

    class FailedWavespeedService:
        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            raise WavespeedFailedError("failed")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(generations, "WavespeedService", FailedWavespeedService)

    bot = SimpleNamespace(send_message=_async_collector(), send_photo=_async_collector(), send_document=_async_collector())
    state = FakeState()

    await generations.poll_generation_result(
        bot=bot,
        user_id=201,
        chat_id=201,
        generation_request_id=generation.id,
        prediction_id="pred-1",
        model_key="nano_banana",
        cost=1,
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
        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            raise WavespeedTimeoutError("timeout")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(generations, "WavespeedService", TimeoutWavespeedService)

    bot = SimpleNamespace(send_message=_async_collector(), send_photo=_async_collector(), send_document=_async_collector())
    state = FakeState()

    await generations.poll_generation_result(
        bot=bot,
        user_id=301,
        chat_id=301,
        generation_request_id=generation.id,
        prediction_id="pred-2",
        model_key="nano_banana",
        cost=1,
        temp_input_path=str(temp_input_path),
    )

    async with session_maker() as session:
        assert await get_user_balance(session, 301) == 5
        assert await get_generation_status(session, generation.id) == GenerationRequestStatus.TIMEOUT


@pytest.mark.asyncio
async def test_polling_does_not_use_or_clear_user_fsm(session_factory, monkeypatch, tmp_path) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=302, balance=4)
        generation = await GenerationRepository(session).create_generation_request(
            user_id=302,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt",
            settings={},
            status="created",
            cost=1,
        )

    temp_input_path = tmp_path / "fsm-independent-input.png"
    temp_input_path.write_bytes(b"input")

    class SuccessfulWavespeedService:
        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            return SimpleNamespace(raw_response={"nsfw_flags": None}, outputs=["https://example.com/output.jpg"])

        async def close(self) -> None:
            return None

    async def fake_send_generation_outputs(*args, **kwargs):
        return generations.OutputDeliveryResult(delivered_successfully=True)

    monkeypatch.setattr(generations, "WavespeedService", SuccessfulWavespeedService)
    monkeypatch.setattr(generations, "send_generation_outputs", fake_send_generation_outputs)

    bot = FakeBot()
    state = FakeState({"prompt": "new flow data", "input_media_items": [{"local_path": "should-not-delete"}]})
    state.state = GenerationStates.waiting_for_prompt

    await generations.poll_generation_result(
        bot=bot,
        user_id=302,
        chat_id=302,
        generation_request_id=generation.id,
        prediction_id="pred-fsm",
        model_key="nano_banana",
        cost=1,
        temp_input_path=str(temp_input_path),
    )

    assert state.state == GenerationStates.waiting_for_prompt
    assert state.data["prompt"] == "new flow data"
    assert temp_input_path.exists() is False

    async with session_maker() as session:
        assert await get_generation_status(session, generation.id) == GenerationRequestStatus.COMPLETED


@pytest.mark.asyncio
async def test_completed_generation_with_empty_outputs_sends_error_and_refunds(session_factory, monkeypatch) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        user = await create_user(session, user_id=305, balance=4)
        user.language_code = "ru"
        await session.commit()
        generation = await GenerationRepository(session).create_generation_request(
            user_id=305,
            chat_id=305,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt",
            settings={},
            status="processing",
            cost=1,
        )

    class EmptyOutputWavespeedService:
        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            return SimpleNamespace(raw_response={"nsfw_flags": None}, outputs=[])

        async def close(self) -> None:
            return None

    monkeypatch.setattr(generations, "WavespeedService", EmptyOutputWavespeedService)

    bot = FakeBot()
    await generations.poll_generation_result(
        bot=bot,
        user_id=305,
        chat_id=305,
        generation_request_id=generation.id,
        prediction_id="pred-empty",
        model_key="nano_banana",
        cost=1,
        temp_input_path=None,
    )

    async with session_maker() as session:
        assert await get_user_balance(session, 305) == 5
        assert await get_generation_status(session, generation.id) == GenerationRequestStatus.FAILED

    assert bot.messages[-1] == "❌ Ошибка E010: провайдер завершил генерацию, но не вернул файл результата. Кредит возвращён."


@pytest.mark.asyncio
async def test_send_generation_outputs_exception_is_not_silent(session_factory, monkeypatch, caplog) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        user = await create_user(session, user_id=306, balance=4)
        user.language_code = "ru"
        await session.commit()
        generation = await GenerationRepository(session).create_generation_request(
            user_id=306,
            chat_id=306,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt",
            settings={},
            status="processing",
            cost=1,
        )

    class SuccessfulWavespeedService:
        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            return SimpleNamespace(raw_response={"nsfw_flags": None}, outputs=["https://example.com/output.jpg"])

        async def close(self) -> None:
            return None

    async def crashing_send_generation_outputs(*args, **kwargs):
        raise RuntimeError("delivery crashed")

    monkeypatch.setattr(generations, "WavespeedService", SuccessfulWavespeedService)
    monkeypatch.setattr(generations, "send_generation_outputs", crashing_send_generation_outputs)

    bot = FakeBot()
    with caplog.at_level(logging.ERROR):
        await generations.poll_generation_result(
            bot=bot,
            user_id=306,
            chat_id=306,
            generation_request_id=generation.id,
            prediction_id="pred-crash",
            model_key="nano_banana",
            cost=1,
            temp_input_path=None,
        )

    async with session_maker() as session:
        assert await get_user_balance(session, 306) == 5
        assert await get_generation_status(session, generation.id) == GenerationRequestStatus.FAILED

    assert bot.messages[-1] == "❌ Ошибка E010: результат был сгенерирован, но бот не смог его доставить. Кредит возвращён."
    assert any(
        isinstance(record.msg, dict)
        and record.msg.get("action") == "background_generation_task_failed"
        and record.msg.get("generation_id") == str(generation.id)
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_startup_recovery_restarts_polling_for_processing_generations(session_factory, monkeypatch) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=307, balance=4)
        generation = await GenerationRepository(session).create_generation_request(
            user_id=307,
            chat_id=777307,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt",
            settings={},
            status="processing",
            wavespeed_prediction_id="pred-recover",
            cost=1,
        )

    captured = {}

    async def fake_poll_generation_result(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(generations, "poll_generation_result", fake_poll_generation_result)

    try:
        recovered_count = await generations.recover_background_generations(FakeBot())
        await await_background_generation_tasks()
    finally:
        generations.BACKGROUND_GENERATIONS.clear()

    assert recovered_count == 1
    assert captured["user_id"] == 307
    assert captured["chat_id"] == 777307
    assert captured["generation_request_id"] == generation.id
    assert captured["prediction_id"] == "pred-recover"
    assert captured["cost"] == 1
    assert captured["temp_input_path"] is None


@pytest.mark.asyncio
async def test_failed_generation_refunds_only_its_cost_and_leaves_other_active(session_factory) -> None:
    session_maker = session_factory
    async with session_maker() as session:
        await create_user(session, user_id=303, balance=5)
        failed_generation = await GenerationRepository(session).create_generation_request(
            user_id=303,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Failed prompt",
            settings={},
            status="processing",
            cost=2,
        )
        other_generation = await GenerationRepository(session).create_generation_request(
            user_id=303,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Other prompt",
            settings={},
            status="processing",
            cost=3,
        )

    monkeypatch_context = pytest.MonkeyPatch()
    monkeypatch_context.setattr(generations.db_manager, "session_factory", session_maker)
    try:
        await generations.mark_generation_failed(
            generation_request_id=failed_generation.id,
            user_id=303,
            model_key="nano_banana",
            cost=2,
            error_message="failed",
            refund_credit=True,
        )
    finally:
        monkeypatch_context.undo()

    async with session_maker() as session:
        assert await get_user_balance(session, 303) == 7
        assert await get_generation_status(session, failed_generation.id) == GenerationRequestStatus.FAILED
        assert await get_generation_status(session, other_generation.id) == GenerationRequestStatus.PROCESSING


@pytest.mark.asyncio
async def test_completed_generation_leaves_other_active_generation_untouched(session_factory, monkeypatch) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=304, balance=5)
        completed_generation = await GenerationRepository(session).create_generation_request(
            user_id=304,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Completed prompt",
            settings={},
            status="processing",
            cost=1,
        )
        other_generation = await GenerationRepository(session).create_generation_request(
            user_id=304,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Other prompt",
            settings={},
            status="processing",
            cost=1,
        )

    await generations.mark_generation_completed(
        generation_request_id=completed_generation.id,
        user_id=304,
        model_key="nano_banana",
        nsfw_flags=None,
        output_count=1,
    )

    async with session_maker() as session:
        assert await get_generation_status(session, completed_generation.id) == GenerationRequestStatus.COMPLETED
        assert await get_generation_status(session, other_generation.id) == GenerationRequestStatus.PROCESSING


@pytest.mark.asyncio
async def test_cleanup_generation_file_removes_multiple_paths(tmp_path) -> None:
    first_path = tmp_path / "cleanup-1.png"
    second_path = tmp_path / "cleanup-2.png"
    first_path.write_bytes(b"one")
    second_path.write_bytes(b"two")

    await generations.cleanup_generation_file([str(first_path), str(second_path)])

    assert first_path.exists() is False
    assert second_path.exists() is False


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_start_submit(session_factory, monkeypatch) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=401, balance=0)

        called = False

        async def fake_poll_generation_result(**kwargs) -> None:
            nonlocal called
            called = True

        async def fake_submit_generation_request(**kwargs) -> str:
            raise AssertionError("submit should not be called without enough balance")

        monkeypatch.setattr(generations, "poll_generation_result", fake_poll_generation_result)
        monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Make the image brighter and keep the subject intact",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "3"},
            }
        )
        callback = FakeCallback(user_id=401)

        await generations.confirm_generation(callback, state, session)

        assert called is False
        assert callback.message.answers[-1] == "❌ Ошибка E006: недостаточно кредитов. Нужно 3, у вас 0."
        assert await get_user_balance(session, 401) == 0


def test_build_confirmation_text_shows_num_generations_and_total_cost() -> None:
    model = generations.get_generation_model("nano_banana")

    text = generations.build_confirmation_text(
        model,
        {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "3"},
        "Generate three variants",
        balance=10,
    )

    assert "Generation count: <code>3</code>" in text
    assert "Cost: 3 credits" in text
    assert "Balance after launch: <code>7</code>" in text


@pytest.mark.asyncio
async def test_confirm_generation_debits_total_cost_and_persists_num_generations(session_factory, monkeypatch) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=451, balance=10)
        captured: dict[str, object] = {}

        async def fake_poll_generation_results_batch(**kwargs) -> None:
            captured.update(kwargs)

        async def fake_submit_generation_request(**kwargs) -> str:
            submit_calls = captured.setdefault("submit_calls", [])
            submit_calls.append(kwargs)
            return f"pred-{len(submit_calls)}"

        monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)
        monkeypatch.setattr(generations, "poll_generation_results_batch", fake_poll_generation_results_batch)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Generate three variants",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "3"},
            }
        )
        callback = FakeCallback(user_id=451)

        class FakeTelegramFilesService:
            def __init__(self, bot):
                self.bot = bot

            async def download_temp_file_and_get_public_url(self, file_id: str):
                return SimpleNamespace(local_path=Path("/tmp/fake-input.png"), public_url="https://example.com/input.png")

        monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)

        await generations.confirm_generation(callback, state, session)

        result = await session.execute(
            select(GenerationRequest).where(GenerationRequest.user_id == 451)
        )
        generation_requests = result.scalars().all()

        assert len(generation_requests) == 3
        assert all(generation.cost == 1 for generation in generation_requests)
        assert all(generation.settings["num_generations"] == "3" for generation in generation_requests)
        assert await get_user_balance(session, 451) == 7
        assert len(captured["generation_predictions"]) == 3
        assert len(captured["submit_calls"]) == 3


@pytest.mark.asyncio
async def test_num_generations_four_starts_four_submit_requests(session_factory, monkeypatch, tmp_path) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=452, balance=10)

        temp_input_path = tmp_path / "input.png"
        temp_input_path.write_bytes(b"input")
        batch_calls: dict[str, object] = {}

        class FakeTelegramFilesService:
            def __init__(self, bot):
                self.bot = bot

            async def download_temp_file_and_get_public_url(self, file_id: str):
                return SimpleNamespace(local_path=temp_input_path, public_url="https://example.com/input.png")

        async def fake_poll_generation_results_batch(**kwargs) -> None:
            batch_calls.update(kwargs)

        async def fake_submit_generation_request(**kwargs) -> str:
            submit_calls = batch_calls.setdefault("submit_calls", [])
            submit_calls.append(kwargs)
            return f"pred-{len(submit_calls)}"

        monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)
        monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)
        monkeypatch.setattr(generations, "poll_generation_results_batch", fake_poll_generation_results_batch)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Generate three variants",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "3"},
            }
        )
        callback = FakeCallback(user_id=452)

        await generations.confirm_generation(callback, state, session)
        await await_background_generation_tasks()

        assert len(batch_calls["generation_predictions"]) == 3
        assert len(batch_calls["submit_calls"]) == 3
        assert callback.message.answers[-1] == "Генерация запущена в фоне. Результат придёт сюда автоматически."


@pytest.mark.asyncio
async def test_batch_failure_refunds_only_one_credit_and_cleans_up_after_all_tasks(session_factory, monkeypatch, tmp_path) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=460, balance=10)
        generation_ids = []
        for _ in range(4):
            generation = await GenerationRepository(session).create_generation_request(
                user_id=460,
                model_key="nano_banana",
                model_endpoint="/api/v3/nano-banana",
                prompt="Prompt",
                settings={"num_generations": "4"},
                status="created",
                cost=1,
            )
            generation_ids.append(generation.id)
        await UserRepository(session).decrease_balance(460, 4)

    temp_input_path = tmp_path / "batch-input.png"
    temp_input_path.write_bytes(b"input")
    prediction_map: dict[str, int] = {}
    generation_predictions = []
    for index, generation_id in enumerate(generation_ids):
        prediction_id = f"pred-{index}"
        prediction_map[prediction_id] = index
        generation_predictions.append((generation_id, prediction_id))

    class MixedWavespeedService:
        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            if prediction_map[prediction_id] == 1:
                raise WavespeedFailedError("failed")
            return SimpleNamespace(raw_response={"nsfw_flags": None}, outputs=[f"https://example.com/{prediction_id}.jpg"])

        async def close(self) -> None:
            return None

    delivery_calls: list[list[str]] = []

    async def fake_send_generation_outputs(*args, **kwargs):
        delivery_calls.append(args[2])
        return generations.OutputDeliveryResult(delivered_successfully=True)

    bot = FakeBot()
    monkeypatch.setattr(generations, "WavespeedService", MixedWavespeedService)
    monkeypatch.setattr(generations, "send_generation_outputs", fake_send_generation_outputs)

    state = FakeState()
    await generations.poll_generation_results_batch(
        bot=bot,
        user_id=460,
        chat_id=460,
        generation_predictions=generation_predictions,
        model_key="nano_banana",
        temp_input_path=str(temp_input_path),
    )

    async with session_maker() as session:
        assert await get_user_balance(session, 460) == 7

    assert len(delivery_calls) == 3
    assert temp_input_path.exists() is False
    assert bot.messages.count("❌ Error E007: one of the generations failed. 1 credit was refunded.") == 1


@pytest.mark.asyncio
async def test_send_generation_outputs_sends_multiple_outputs(monkeypatch, tmp_path) -> None:
    first_output_path = tmp_path / "imai-1.jpg"
    second_output_path = tmp_path / "imai-2.jpg"
    first_output_path.write_bytes(b"image-1")
    second_output_path.write_bytes(b"image-2")
    output_map = {
        "https://example.com/output-1.jpg": (str(first_output_path), "image/jpeg", 7),
        "https://example.com/output-2.jpg": (str(second_output_path), "image/jpeg", 8),
    }

    async def fake_download_output_file_to_temp(output_url: str):
        return output_map[output_url]

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)

    delivered = await generations.send_generation_outputs(
        bot,
        1,
        ["https://example.com/output-1.jpg", "https://example.com/output-2.jpg"],
    )

    assert delivered.delivered_successfully is True
    assert len(bot.photos) == 2


def _async_collector():
    async def _call(*args, **kwargs):
        return None

    return _call


def test_log_generation_output_delivery_uses_only_safe_fields(monkeypatch) -> None:
    payloads: list[dict[str, object]] = []

    def fake_info(payload):
        payloads.append(payload)

    monkeypatch.setattr(generations.logger, "info", fake_info)

    generations.log_generation_output_delivery(
        "photo",
        user_id=123,
        send_results_as_files=False,
        content_type="image/jpeg; charset=binary",
        file_size_bytes=123,
        status="success",
    )

    assert payloads == [
        {
            "action": "generation_output_delivery",
            "user_id": 123,
            "send_results_as_files": False,
            "content_type": "image/jpeg",
            "delivery_method": "photo",
            "file_size": 123,
            "status": "success",
        }
    ]


def test_normalize_filename_replaces_wavespeed_prefix() -> None:
    assert generations.normalize_filename("wavespeed-output-abc.jpg") == "imai-abc.jpg"


@pytest.mark.asyncio
async def test_download_file_from_url_uses_imai_filename(monkeypatch) -> None:
    class FakeResponse:
        headers = {"content-type": "image/jpeg", "content-length": "4"}

        def raise_for_status(self) -> None:
            return None

        class Content:
            async def iter_chunked(self, chunk_size):
                yield b"test"

        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeClientSession:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def get(self, url: str, allow_redirects: bool = True):
            return FakeResponse()

    monkeypatch.setattr(generations.aiohttp, "ClientSession", FakeClientSession)

    temp_output_path = await generations.download_file_from_url(
        "https://example.com/files/wavespeed-output-abc.jpg?token=1"
    )

    try:
        assert Path(temp_output_path).name == "imai-abc.jpg"
        assert Path(temp_output_path).exists() is True
        assert Path(temp_output_path).read_bytes() == b"test"
    finally:
        Path(temp_output_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_download_output_file_to_temp_returns_metadata_from_downloaded_file(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-abc.jpg"
    output_path.write_bytes(b"test")

    async def fake_download_file_from_url(url: str) -> str:
        return str(output_path)

    monkeypatch.setattr(generations, "download_file_from_url", fake_download_file_from_url)

    temp_output_path, content_type, file_size_bytes = await generations.download_output_file_to_temp(
        "https://example.com/files/wavespeed-output-abc.jpg?token=1"
    )

    assert temp_output_path == str(output_path)
    assert content_type == "image/jpeg"
    assert file_size_bytes == 4


@pytest.mark.asyncio
async def test_send_generation_outputs_notifies_when_file_too_large(monkeypatch, tmp_path) -> None:
    oversized_path = tmp_path / "imai-large.mp4"
    oversized_path.write_bytes(b"large")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(oversized_path), "video/mp4", generations.get_safe_telegram_document_size_bytes() + 1

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"])

    assert delivered.delivered_successfully is False
    assert delivered.use_r2 is True
    assert bot.messages[-1] == "Could not upload the file. Please try again later"
    assert bot.documents == []


@pytest.mark.asyncio
async def test_send_generation_outputs_returns_use_r2_for_files_over_safe_limit(monkeypatch, tmp_path) -> None:
    safe_limit_path = tmp_path / "imai-safe-limit.mp4"
    safe_limit_path.write_bytes(b"safe-limit")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(safe_limit_path), "video/mp4", generations.get_safe_telegram_document_size_bytes() + 1

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"])

    assert delivered.delivered_successfully is False
    assert delivered.use_r2 is True
    assert bot.documents == []
    assert bot.messages[-1] == "Could not upload the file. Please try again later"


@pytest.mark.asyncio
async def test_send_generation_outputs_uses_r2_fallback_when_configured(monkeypatch, tmp_path) -> None:
    safe_limit_path = tmp_path / "imai-safe-limit.mp4"
    safe_limit_path.write_bytes(b"safe-limit")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(safe_limit_path), "video/mp4", generations.get_safe_telegram_document_size_bytes() + 1

    class FakeR2StorageService:
        def is_configured(self) -> bool:
            return True

        def upload_and_get_object_key(self, local_path: str, filename: str, content_type: str | None) -> str:
            assert local_path == str(safe_limit_path)
            assert filename == safe_limit_path.name
            assert content_type == "video/mp4"
            return "temporary-outputs/test/file.mp4"

    class FakeDownloadLinkService:
        async def create_short_download_url(
            self,
            r2_object_key: str,
            *,
            filename=None,
            file_size_bytes=None,
            content_type=None,
            expires_at=None,
        ) -> str:
            assert r2_object_key == "temporary-outputs/test/file.mp4"
            assert filename == safe_limit_path.name
            assert file_size_bytes == generations.get_safe_telegram_document_size_bytes() + 1
            assert content_type == "video/mp4"
            return "https://example.com/d/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "R2StorageService", FakeR2StorageService)
    monkeypatch.setattr(generations, "DownloadLinkService", FakeDownloadLinkService)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"])

    assert delivered.delivered_successfully is True
    assert delivered.use_r2 is True
    assert bot.documents == []
    assert bot.messages[-1] == (
        "⚠️ File is too large for Telegram\n\n"
        "Uploaded to secure Cloudflare R2 storage\n\n"
        "🔗 Download file:\nhttps://example.com/d/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN\n\n"
        "🔒 Safe link\n\n"
        "If you are unsure, you can inspect the link with any AI, an online link analyzer, or open it in a browser."
    )


@pytest.mark.asyncio
async def test_send_document_with_retry_retries_network_errors(monkeypatch, tmp_path) -> None:
    file_path = tmp_path / "imai-test.jpg"
    file_path.write_bytes(b"test")
    bot = FakeBot()
    attempts = {"count": 0}
    sleep_calls: list[int] = []

    async def flaky_send_document(chat_id, document, caption=None, reply_markup=None, request_timeout=None):
        attempts["count"] += 1
        if attempts["count"] < 4:
            raise TimeoutError("timeout")
        await FakeBot.send_document(bot, chat_id, document, caption, reply_markup, request_timeout)

    async def fake_sleep(seconds: int):
        sleep_calls.append(seconds)

    monkeypatch.setattr(bot, "send_document", flaky_send_document)
    monkeypatch.setattr(generations.asyncio, "sleep", fake_sleep)

    await generations.send_document_with_retry(bot=bot, chat_id=1, file_path=str(file_path), caption=None)

    assert attempts["count"] == 4
    assert sleep_calls == [1, 2, 4]
    assert bot.documents[-1]["request_timeout"] == 3600


@pytest.mark.asyncio
async def test_send_generation_outputs_deletes_temp_file_only_after_document_send(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-send.jpg"
    output_path.write_bytes(b"image")
    captured = {}
    payloads = []

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/jpeg", 5

    async def fake_send_document_with_retry(*, bot, chat_id: int, file_path: str, caption=None, reply_markup=None):
        captured["exists_during_send"] = Path(file_path).exists()

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return True

    def fake_info(payload):
        payloads.append(payload)

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "send_document_with_retry", fake_send_document_with_retry)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)
    monkeypatch.setattr(generations.logger, "info", fake_info)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.jpg"], user_id=1)

    assert delivered.delivered_successfully is True
    assert captured["exists_during_send"] is True
    assert output_path.exists() is False
    assert payloads[-1] == {
        "delivery_method": "cleanup",
        "content_type": None,
        "file_size_bytes": None,
    }


@pytest.mark.asyncio
async def test_send_generation_outputs_deletes_temp_file_only_after_r2_upload(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-r2.mp4"
    output_path.write_bytes(b"video")
    captured = {}
    payloads = []

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "video/mp4", generations.get_safe_telegram_document_size_bytes() + 1

    class FakeR2StorageService:
        def is_configured(self) -> bool:
            return True

        def upload_and_get_object_key(self, local_path: str, filename: str, content_type: str | None) -> str:
            captured["exists_during_r2_upload"] = Path(local_path).exists()
            return "temporary-outputs/test/file.mp4"

    class FakeDownloadLinkService:
        async def create_short_download_url(
            self,
            r2_object_key: str,
            *,
            filename=None,
            file_size_bytes=None,
            content_type=None,
            expires_at=None,
        ) -> str:
            return "https://example.com/d/token"

    def fake_info(payload):
        payloads.append(payload)

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "R2StorageService", FakeR2StorageService)
    monkeypatch.setattr(generations, "DownloadLinkService", FakeDownloadLinkService)
    monkeypatch.setattr(generations.logger, "info", fake_info)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"])

    assert delivered.use_r2 is True
    assert captured["exists_during_r2_upload"] is True
    assert output_path.exists() is False
    assert payloads[-1] == {
        "delivery_method": "cleanup",
        "content_type": None,
        "file_size_bytes": None,
    }


@pytest.mark.asyncio
async def test_send_generation_outputs_uses_r2_after_telegram_delivery_failure(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-failed-delivery.jpg"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/jpeg", 5

    async def fake_send_document_with_retry(*, bot, chat_id: int, file_path: str, caption=None, reply_markup=None):
        raise ConnectionResetError("delivery failed")

    async def fake_send_photo_output(*, bot, chat_id: int, file_path: str, reply_markup=None):
        raise TelegramBadRequest("photo failed")

    class FakeR2StorageService:
        def is_configured(self) -> bool:
            return True

        def upload_and_get_object_key(self, local_path: str, filename: str, content_type: str | None) -> str:
            assert local_path == str(output_path)
            assert filename == output_path.name
            assert content_type == "image/jpeg"
            return "temporary-outputs/test/retry-fallback.jpg"

    class FakeDownloadLinkService:
        async def create_short_download_url(
            self,
            r2_object_key: str,
            *,
            filename=None,
            file_size_bytes=None,
            content_type=None,
            expires_at=None,
        ) -> str:
            assert r2_object_key == "temporary-outputs/test/retry-fallback.jpg"
            assert filename == output_path.name
            assert file_size_bytes == 5
            assert content_type == "image/jpeg"
            return "https://example.com/d/retry-fallback-token"

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "send_photo_output", fake_send_photo_output)
    monkeypatch.setattr(generations, "send_document_with_retry", fake_send_document_with_retry)
    monkeypatch.setattr(generations, "R2StorageService", FakeR2StorageService)
    monkeypatch.setattr(generations, "DownloadLinkService", FakeDownloadLinkService)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.jpg"])

    assert delivered.delivered_successfully is True
    assert delivered.use_r2 is True
    assert bot.documents == []
    assert bot.messages[-1] == (
        "⚠️ File is too large for Telegram\n\n"
        "Uploaded to secure Cloudflare R2 storage\n\n"
        "🔗 Download file:\nhttps://example.com/d/retry-fallback-token\n\n"
        "🔒 Safe link\n\n"
        "If you are unsure, you can inspect the link with any AI, an online link analyzer, or open it in a browser."
    )


@pytest.mark.asyncio
async def test_send_generation_outputs_reports_error_when_r2_upload_fails(monkeypatch, tmp_path) -> None:
    safe_limit_path = tmp_path / "imai-safe-limit.mp4"
    safe_limit_path.write_bytes(b"safe-limit")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(safe_limit_path), "video/mp4", generations.get_safe_telegram_document_size_bytes() + 1

    class FakeR2StorageService:
        def is_configured(self) -> bool:
            return True

        def upload_and_get_object_key(self, local_path: str, filename: str, content_type: str | None) -> str:
            raise RuntimeError("upload failed")

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "R2StorageService", FakeR2StorageService)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"])

    assert delivered.delivered_successfully is False
    assert delivered.use_r2 is True
    assert bot.documents == []
    assert bot.messages[-1] == "Could not upload the file. Please try again later"


@pytest.mark.asyncio
async def test_send_generation_outputs_reports_error_when_r2_returns_empty_url(monkeypatch, tmp_path) -> None:
    safe_limit_path = tmp_path / "imai-safe-limit.mp4"
    safe_limit_path.write_bytes(b"safe-limit")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(safe_limit_path), "video/mp4", generations.get_safe_telegram_document_size_bytes() + 1

    class FakeR2StorageService:
        def is_configured(self) -> bool:
            return True

        def upload_and_get_object_key(self, local_path: str, filename: str, content_type: str | None) -> str:
            return "temporary-outputs/test/file.mp4"

    class FakeDownloadLinkService:
        async def create_short_download_url(
            self,
            r2_object_key: str,
            *,
            filename=None,
            file_size_bytes=None,
            content_type=None,
            expires_at=None,
        ) -> str:
            return ""

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "R2StorageService", FakeR2StorageService)
    monkeypatch.setattr(generations, "DownloadLinkService", FakeDownloadLinkService)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"])

    assert delivered.delivered_successfully is False
    assert delivered.use_r2 is True
    assert bot.documents == []
    assert bot.messages[-1] == "Could not upload the file. Please try again later"


@pytest.mark.asyncio
async def test_send_generation_outputs_reports_plain_message_when_telegram_delivery_fails_without_r2(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-failed-delivery.jpg"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/jpeg", 5

    async def fake_send_document_with_retry(*, bot, chat_id: int, file_path: str, caption=None, reply_markup=None):
        raise TimeoutError("delivery failed")

    async def fake_send_photo_output(*, bot, chat_id: int, file_path: str, reply_markup=None):
        raise TelegramBadRequest("photo failed")

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "send_photo_output", fake_send_photo_output)
    monkeypatch.setattr(generations, "send_document_with_retry", fake_send_document_with_retry)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.jpg"])

    assert delivered.delivered_successfully is False
    assert delivered.use_r2 is False
    assert bot.documents == []
    assert bot.messages[-1] == "The file is ready, but Telegram could not deliver it"


@pytest.mark.asyncio
async def test_new_user_has_send_results_as_files_disabled(session_factory) -> None:
    async with session_factory() as session:
        user = await create_user(session, user_id=501, balance=3)

        assert user.send_results_as_files is False
        assert await get_user_delivery_preference(session, 501) is False


@pytest.mark.asyncio
async def test_toggle_user_delivery_preference_changes_value(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=502, balance=3)
        repo = UserRepository(session)

        assert await repo.get_user_delivery_preference(502) is False
        assert await repo.toggle_user_delivery_preference(502) is True
        assert await repo.get_user_delivery_preference(502) is True
        assert await repo.set_user_delivery_preference(502, False) is True
        assert await repo.get_user_delivery_preference(502) is False


@pytest.mark.asyncio
async def test_send_generation_outputs_sends_image_as_photo_by_default(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-photo.jpg"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/jpeg", 5

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return False

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.jpg"])

    assert delivered.delivered_successfully is True
    assert bot.photos != []
    assert bot.videos == []
    assert bot.documents == []


@pytest.mark.asyncio
async def test_send_generation_outputs_sends_png_as_photo_when_preference_disabled(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-photo.png"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/png", 5

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return False

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.png"], user_id=1)

    assert delivered.delivered_successfully is True
    assert bot.photos != []
    assert bot.documents == []


@pytest.mark.asyncio
async def test_send_generation_outputs_sends_video_as_video_by_default(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-video.mp4"
    output_path.write_bytes(b"video")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "video/mp4", 5

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return False

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"])

    assert delivered.delivered_successfully is True
    assert bot.photos == []
    assert bot.videos != []
    assert bot.documents == []


@pytest.mark.asyncio
async def test_send_generation_outputs_sends_video_as_video_when_preference_disabled(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-video.mp4"
    output_path.write_bytes(b"video")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "video/mp4", 5

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return False

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"], user_id=1)

    assert delivered.delivered_successfully is True
    assert bot.videos != []
    assert bot.documents == []


@pytest.mark.asyncio
async def test_send_generation_outputs_sends_image_as_document_when_preference_enabled(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-image.jpg"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/jpeg", 5

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return True

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.jpg"], user_id=1)

    assert delivered.delivered_successfully is True
    assert bot.documents != []
    assert bot.photos == []
    assert bot.videos == []


@pytest.mark.asyncio
async def test_send_generation_outputs_sends_video_as_document_when_preference_enabled(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-video.mp4"
    output_path.write_bytes(b"video")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "video/mp4", 5

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return True

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"], user_id=1)

    assert delivered.delivered_successfully is True
    assert bot.documents != []
    assert bot.photos == []
    assert bot.videos == []


@pytest.mark.asyncio
async def test_send_generation_outputs_falls_back_from_photo_to_document(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-image.jpg"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/jpeg", 5

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return False

    async def failing_send_photo(chat_id, photo, caption=None, reply_markup=None):
        raise TelegramBadRequest("photo failed")

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)
    monkeypatch.setattr(bot, "send_photo", failing_send_photo)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.jpg"])

    assert delivered.delivered_successfully is True
    assert bot.documents != []


@pytest.mark.asyncio
async def test_send_generation_outputs_falls_back_from_video_to_document(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-video.mp4"
    output_path.write_bytes(b"video")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "video/mp4", 5

    async def fake_get_user_send_results_as_files(user_id: int) -> bool:
        return False

    async def failing_send_video(chat_id, video, caption=None, reply_markup=None, request_timeout=None):
        raise TelegramBadRequest("video failed")

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations, "get_user_send_results_as_files", fake_get_user_send_results_as_files)
    monkeypatch.setattr(bot, "send_video", failing_send_video)

    delivered = await generations.send_generation_outputs(bot, 1, ["https://example.com/output.mp4"])

    assert delivered.delivered_successfully is True
    assert bot.documents != []


@pytest.mark.asyncio
async def test_cleanup_temp_output_file_does_not_fail_if_file_already_deleted(monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-missing.jpg"
    output_path.write_bytes(b"image")
    output_path.unlink()
    payloads = []

    def fake_info(payload):
        payloads.append(payload)

    monkeypatch.setattr(generations.logger, "info", fake_info)

    await generations.cleanup_temp_output_file(str(output_path))

    assert payloads == [
        {
            "delivery_method": "cleanup",
            "content_type": None,
            "file_size_bytes": None,
        }
    ]


@pytest.mark.asyncio
async def test_poll_generation_result_marks_delivery_failed_when_document_delivery_fails(session_factory, monkeypatch) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        user = await create_user(session, user_id=501, balance=4)
        user.language_code = "ru"
        await session.commit()
        generation = await GenerationRepository(session).create_generation_request(
            user_id=501,
            chat_id=501,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt",
            settings={},
            status="created",
            cost=1,
        )

    class SuccessfulWavespeedService:
        async def submit_generation(self, model_key: str, payload: dict[str, object]):
            return SimpleNamespace(prediction_id="pred-complete")

        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            return SimpleNamespace(raw_response={"nsfw_flags": None}, outputs=["https://example.com/output.jpg"])

        async def close(self) -> None:
            return None

    async def fake_send_generation_outputs(*args, **kwargs):
        return generations.OutputDeliveryResult(
            success=False,
            method="document",
            error_code=generations.ErrorCode.E009_TELEGRAM_DELIVERY_FAILED,
            error_message="Telegram delivery failed",
        )

    monkeypatch.setattr(generations, "WavespeedService", SuccessfulWavespeedService)
    monkeypatch.setattr(generations, "send_generation_outputs", fake_send_generation_outputs)

    bot = FakeBot()
    state = FakeState()

    await generations.poll_generation_result(
        bot=bot,
        user_id=501,
        chat_id=501,
        generation_request_id=generation.id,
        prediction_id="pred-complete",
        model_key="nano_banana",
        cost=1,
        temp_input_path=None,
    )

    async with session_maker() as session:
        assert await get_generation_status(session, generation.id) == GenerationRequestStatus.DELIVERY_FAILED
        assert await get_user_balance(session, 501) == 5

    assert bot.messages[-1] == "❌ Ошибка E009: файл готов, но Telegram не смог его доставить. Кредит возвращён."