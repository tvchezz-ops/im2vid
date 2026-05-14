"""Tests for generation balance debit and refund flow."""

from __future__ import annotations

import logging
import os
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
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
from app.i18n import t
from app.services.generation_service import GenerationModel, GenerationSetting, MODEL_REGISTRY, SettingOption
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
        self.media_group_id = None
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


TEXT_SETTING_MODEL_KEY = "test_text_setting_model"
AUDIO_NO_LIMIT_MODEL_KEY = "test_audio_no_limit_model"


def install_text_setting_model() -> None:
    MODEL_REGISTRY[TEXT_SETTING_MODEL_KEY] = GenerationModel(
        key=TEXT_SETTING_MODEL_KEY,
        title="Test Text Setting Model",
        provider="google",
        generation_type="text_to_video",
        endpoint="https://api.wavespeed.ai/api/v3/google/test-text-setting-model",
        docs_url="https://wavespeed.ai/docs/docs-api/google/test-text-setting-model",
        description="Test model with text setting",
        max_images=0,
        requires_prompt=True,
        requires_image=False,
        requires_video=False,
        requires_audio=False,
        outputs="video",
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "note"),
        user_settings={
            "note": GenerationSetting(
                key="note",
                title="Заметка",
                type="text",
                default="",
                options=(),
                description="Дополнительное описание",
            ),
            "num_generations": GenerationSetting(
                key="num_generations",
                title="Количество генераций",
                type="select",
                default="1",
                options=(SettingOption(value="1", label="1"),),
            ),
        },
    )


def remove_text_setting_model() -> None:
    MODEL_REGISTRY.pop(TEXT_SETTING_MODEL_KEY, None)


def install_audio_no_limit_model() -> None:
    MODEL_REGISTRY[AUDIO_NO_LIMIT_MODEL_KEY] = GenerationModel(
        key=AUDIO_NO_LIMIT_MODEL_KEY,
        title="Test Audio No Limit Model",
        provider="kling",
        generation_type="lipsync",
        endpoint="https://api.wavespeed.ai/api/v3/test/audio-no-limit",
        docs_url="https://wavespeed.ai/docs/docs-api/test/audio-no-limit",
        description="Test model with audio input and no docs max size",
        max_images=0,
        requires_prompt=False,
        requires_image=False,
        requires_video=True,
        requires_audio=True,
        outputs="video",
        required_payload_fields=("video", "audio"),
        allowed_payload_fields=("video", "audio"),
        input_requirements={
            "prompt": {"required": False, "payload_field": "prompt"},
            "video": {"required": True, "payload_field": "video"},
            "audio": {"required": True, "payload_field": "audio"},
        },
        user_settings={},
    )


def remove_audio_no_limit_model() -> None:
    MODEL_REGISTRY.pop(AUDIO_NO_LIMIT_MODEL_KEY, None)


class FakeBot:
    def __init__(self):
        self.documents: list[dict[str, object]] = []
        self.photos: list[dict[str, object]] = []
        self.videos: list[dict[str, object]] = []
        self.messages: list[str] = []
        self.message_markups: list[object] = []
        self.message_parse_modes: list[str | None] = []

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

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.messages.append(text)
        self.message_markups.append(reply_markup)
        self.message_parse_modes.append(parse_mode)


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


async def get_generation_count(session, user_id: int) -> int:
    result = await session.execute(select(GenerationRequest).where(GenerationRequest.user_id == user_id))
    return len(result.scalars().all())


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
    assert f"{t('generation.choose_type', 'en')}:" in message.answers[-1]
    assert "Text to Video" in message.answers[-1]
    keyboard = message.answer_markups[-1]
    callback_data = [row[0].callback_data for row in keyboard.inline_keyboard[:-1]]
    assert "gen:section:lipsync" in callback_data


@pytest.mark.asyncio
async def test_show_generation_menu_ignores_active_generation_count(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=421, balance=5)
        for index in range(3):
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
        assert f"{t('generation.choose_type', 'ru')}:" in message.answers[-1]
        removed_warning_fragment = "Можно запускать" + " не больше"
        assert not any(removed_warning_fragment in answer for answer in message.answers)


@pytest.mark.asyncio
@pytest.mark.parametrize("active_generation_count", [0, 999])
async def test_confirm_generation_allows_four_generations_with_existing_active_requests(
    session_factory,
    monkeypatch,
    tmp_path,
    active_generation_count: int,
) -> None:
    async def fake_submit_generation_request(**kwargs) -> str:
        submit_calls.append(kwargs)
        return f"pred-no-limit-{len(submit_calls)}"

    async def fake_poll_generation_results_batch(**kwargs) -> None:
        batch_calls.update(kwargs)
        return None

    class FakeTelegramFilesService:
        def __init__(self, bot):
            self.bot = bot

        async def download_temp_file_and_get_public_url(self, file_id: str):
            return SimpleNamespace(local_path=tmp_path / "input.png", public_url="https://example.com/input.png")

    submit_calls: list[dict[str, object]] = []
    batch_calls: dict[str, object] = {}
    monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)
    monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)
    monkeypatch.setattr(generations, "poll_generation_results_batch", fake_poll_generation_results_batch)

    async with session_factory() as session:
        await create_user(session, user_id=422, balance=100)
        for index in range(active_generation_count):
            await GenerationRepository(session).create_generation_request(
                user_id=422,
                model_key="nano_banana",
                model_endpoint="/api/v3/nano-banana",
                prompt=f"Prompt {index}",
                settings={},
                status="processing",
                cost=1,
            )

        menu_message = FakeMessage(chat_id=422)
        menu_state = FakeState()
        await generations.show_generation_menu(menu_message, menu_state, session)

        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Make the image brighter and keep the subject intact",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "4"},
            }
        )
        callback = FakeCallback(user_id=422)

        await generations.confirm_generation(callback, state, session)
        await await_background_generation_tasks()

        assert menu_state.state == GenerationStates.choosing_generation_type
        assert f"{t('generation.choose_type', 'ru')}:" in menu_message.answers[-1]
        assert state.state is None
        assert callback.message.answers[-1] == t("generation.started", "ru")
        assert len(submit_calls) == 4
        assert len(batch_calls["generation_predictions"]) == 4
        assert await get_user_balance(session, 422) == 32
        all_answers = [*menu_message.answers, *callback.message.answers]
        removed_warning_fragment = "Можно запускать" + " не больше"
        assert not any(removed_warning_fragment in answer for answer in all_answers)
        assert not any("Дождитесь завершения" in answer for answer in all_answers)


@pytest.mark.asyncio
async def test_submit_failure_refunds_debited_credits(session_factory, monkeypatch, tmp_path) -> None:
    class FakeTelegramFilesService:
        def __init__(self, bot):
            self.bot = bot

        async def download_temp_file_and_get_public_url(self, file_id: str):
            return SimpleNamespace(local_path=tmp_path / "input.png", public_url="https://example.com/input.png")

    async def fake_submit_generation_request(**kwargs) -> str:
        raise WavespeedFailedError("bad request")

    monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)
    monkeypatch.setattr(generations, "submit_generation_request", fake_submit_generation_request)

    async with session_factory() as session:
        await create_user(session, user_id=423, balance=100)
        state = FakeState(
            {
                "model_key": "nano_banana",
                "model_title": "Nano Banana",
                "model_endpoint": "/api/v3/nano-banana",
                "prompt": "Make the image brighter and keep the subject intact",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "1"},
            }
        )
        callback = FakeCallback(user_id=423)

        await generations.confirm_generation(callback, state, session)

        assert await get_user_balance(session, 423) == 100
        assert state.state is None
        assert any("Что-то пошло не так" in answer for answer in callback.message.answers)


@pytest.mark.asyncio
async def test_choose_generation_section_shows_models_for_type() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=402)
    callback = FakeCallback(user_id=402, message=message, data="gen:section:image_edit")

    await generations.choose_generation_section(callback, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert state.data["selected_generation_type"] == "image_edit"
    assert state.data["selected_provider"] is None
    assert message.edits[-1] == f"{t('generation.choose_model', 'ru')}:"


@pytest.mark.asyncio
async def test_choose_all_models_shows_provider_list() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=403)
    callback = FakeCallback(user_id=403, message=message, data="gen:all")

    await generations.show_all_generation_providers(callback, state)

    assert state.state == GenerationStates.choosing_provider
    assert state.data["selected_generation_type"] == "all"
    assert state.data["selected_provider"] is None
    assert message.edits[-1] == t("generation.choose_provider", "ru")


@pytest.mark.asyncio
async def test_choose_provider_shows_provider_models() -> None:
    state = FakeState({"selected_generation_type": "all"})
    message = FakeMessage(chat_id=404)
    callback = FakeCallback(user_id=404, message=message, data="gen:provider:google")

    await generations.choose_provider(callback, state)

    assert state.state == GenerationStates.choosing_provider
    assert state.data["selected_provider"] == "google"
    assert message.edits[-1] == f"{t('generation.choose_model', 'ru')}:"


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
    assert message.edits[-1] == f"{t('generation.choose_model', 'ru')}:"
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
    assert f"{t('generation.choose_type', 'ru')}:" in message.edits[-1]
    assert t("generation.section_title.text_to_video", "ru") in message.edits[-1]


@pytest.mark.asyncio
async def test_unknown_generation_callback_shows_fallback_alert_screen() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=406)
    callback = FakeCallback(user_id=406, message=message, data="gen:obsolete")

    await generations.handle_unknown_generation_callback(callback, state)

    assert state.state == GenerationStates.choosing_generation_type
    assert f"{t('generation.choose_type', 'ru')}:" in message.edits[-1]


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
    assert t("settings.parameter", "ru", parameter="Формат") in message.edits[-1]
    assert t("settings.current_value", "ru", value="1:1") in message.edits[-1]
    assert t("settings.select_helper", "ru") in message.edits[-1]
    assert "Варианты:" not in message.edits[-1]
    assert "• <code>1:1</code>" not in message.edits[-1]
    assert "• <code>16:9</code>" not in message.edits[-1]
    assert message.edit_markups[-1] is not None

    choose_callback = FakeCallback(user_id=450, message=message, data="gen:set:aspect_ratio:8")
    await generations.choose_setting_value(choose_callback, state)

    assert state.data["user_settings"]["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_open_setting_selector_for_text_setting_switches_to_text_input() -> None:
    install_text_setting_model()
    try:
        state = FakeState(
            {
                "model_key": TEXT_SETTING_MODEL_KEY,
                "user_settings": {"note": ""},
            }
        )
        message = FakeMessage(chat_id=451)
        callback = FakeCallback(user_id=451, message=message, data="gen:setting:note")

        await generations.open_setting_selector(callback, state)

        assert state.state == GenerationStates.waiting_for_setting_text
        assert state.data["current_setting_key"] == "note"
        assert t("settings.parameter", "ru", parameter="Заметка") in message.edits[-1]
    finally:
        remove_text_setting_model()


@pytest.mark.asyncio
async def test_process_text_setting_value_saves_text_and_returns_to_settings() -> None:
    install_text_setting_model()
    try:
        state = FakeState(
            {
                "model_key": TEXT_SETTING_MODEL_KEY,
                "current_setting_key": "note",
                "user_settings": {"note": ""},
            }
        )
        state.state = GenerationStates.waiting_for_setting_text
        message = FakeMessage(chat_id=452)
        message.text = "blur, noise"

        await generations.process_text_setting_value(message, state)

        assert state.state == GenerationStates.choosing_settings
        assert state.data["user_settings"]["note"] == "blur, noise"
        assert message.answers[0] == t("generation.value_saved", "ru")
    finally:
        remove_text_setting_model()


@pytest.mark.asyncio
async def test_process_text_setting_value_clears_dash_to_empty_string() -> None:
    install_text_setting_model()
    try:
        state = FakeState(
            {
                "model_key": TEXT_SETTING_MODEL_KEY,
                "current_setting_key": "note",
                "user_settings": {"note": "existing"},
            }
        )
        state.state = GenerationStates.waiting_for_setting_text
        message = FakeMessage(chat_id=453)
        message.text = "-"

        await generations.process_text_setting_value(message, state)

        assert state.state == GenerationStates.choosing_settings
        assert state.data["user_settings"]["note"] == ""
    finally:
        remove_text_setting_model()


@pytest.mark.asyncio
async def test_continue_after_settings_shows_lipsync_media_prompt() -> None:
    state = FakeState({"model_generation_type": "lipsync"})
    message = FakeMessage(chat_id=407)
    callback = FakeCallback(user_id=407, message=message, data="gen:continue")

    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_image
    assert message.edits[-1] == t("generation.flow.lipsync.initial", "ru")

@pytest.mark.asyncio
async def test_continue_after_settings_for_text_to_image_goes_to_prompt() -> None:
    state = FakeState({"model_key": "alibaba_wan_2_6_text_to_image", "model_generation_type": "text_to_image"})
    message = FakeMessage(chat_id=470)
    callback = FakeCallback(user_id=470, message=message, data="gen:continue")

    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_prompt
    assert message.edits[-1] == t("generation.flow.text_to_image.initial", "ru")

@pytest.mark.asyncio
async def test_continue_after_settings_for_video_edit_goes_to_video_step() -> None:
    state = FakeState({"model_key": "google_veo3_1_fast_video_extend", "model_generation_type": "video_edit"})
    message = FakeMessage(chat_id=471)
    callback = FakeCallback(user_id=471, message=message, data="gen:continue")

    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_video
    assert message.edits[-1] == t(
        "generation.send_video_for_model",
        "ru",
        model="Google Veo3.1 Fast Video Extend",
    )


@pytest.mark.asyncio
async def test_generation_image_step_uses_db_language_without_mixing(session_factory) -> None:
    async with session_factory() as session:
        user = await create_user(session, user_id=481, balance=100)
        user.language_code = "ru"
        await session.commit()

        state = FakeState({"model_key": "alibaba_wan_2_7_image_to_video", "model_generation_type": "image_to_video"})
        message = FakeMessage(chat_id=481)
        callback = FakeCallback(user_id=481, message=message, data="gen:continue")
        callback.from_user.language_code = "en"
        message.from_user.language_code = "en"

        await generations.continue_after_settings(callback, state, session)

        combined_text = "\n".join(message.edits + message.answers)
        assert t("generation.send_image_for_model", "ru", model="Alibaba Wan 2.7 Image To Video") in combined_text
        assert t("generation.changed_mind_back_to_settings", "ru") in combined_text
        assert "Send an image" not in combined_text
        assert "If you changed your mind" not in combined_text
        assert message.answer_markups[-1].keyboard[0][0].text == f"⬅️ {t('generation.back_to_settings', 'ru')}"


@pytest.mark.asyncio
async def test_generation_image_step_english_user_has_no_russian_phrases(session_factory) -> None:
    async with session_factory() as session:
        user = await create_user(session, user_id=482, balance=100)
        user.language_code = "en"
        await session.commit()

        state = FakeState({"model_key": "alibaba_wan_2_7_image_to_video", "model_generation_type": "image_to_video"})
        message = FakeMessage(chat_id=482)
        callback = FakeCallback(user_id=482, message=message, data="gen:continue")
        callback.from_user.language_code = "ru"
        message.from_user.language_code = "ru"

        await generations.continue_after_settings(callback, state, session)

        combined_text = "\n".join(message.edits + message.answers)
        assert t("generation.send_image_for_model", "en", model="Alibaba Wan 2.7 Image To Video") in combined_text
        assert t("generation.changed_mind_back_to_settings", "en") in combined_text
        assert "Отправьте" not in combined_text
        assert "Если передумали" not in combined_text
        assert message.answer_markups[-1].keyboard[0][0].text == f"⬅️ {t('generation.back_to_settings', 'en')}"


@pytest.mark.asyncio
async def test_continue_after_settings_for_multi_image_model_goes_to_images_step() -> None:
    state = FakeState({"model_key": "nano_banana", "model_generation_type": "image_edit"})
    message = FakeMessage(chat_id=476)
    callback = FakeCallback(user_id=476, message=message, data="gen:continue")

    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_images
    assert message.edits[-1] == t(
        "generation.send_images_for_model",
        "ru",
        model="Google Nano Banana Pro Edit Ultra",
        min_count=1,
        max_count=10,
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
    assert message.answers[-1] == t("generation.invalid_wait_image", "ru")


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
    assert state.data["input_media_urls"] == ["https://example.com/photo-file-id.png"]
    assert state.data["input_media_paths"] == ["/tmp/photo-file-id.png"]
    assert state.data["input_media_file_ids"] == ["photo-file-id"]
    assert state.data["input_media_items"][0]["public_url"] == "https://example.com/photo-file-id.png"
    assert t("generation.images_uploaded_progress", "ru", count=1, max_count=10) in message.answers[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_key", "model_title", "max_images_text"),
    [
        ("nano_banana", "Google Nano Banana Pro Edit Ultra", "1 из 10"),
        ("seedream", "Bytedance Seedream V4.5 Edit", "1 из 10"),
    ],
)
async def test_legacy_nano_banana_and_seedream_generation_flow_still_reaches_confirmation(
    session_factory,
    monkeypatch,
    model_key: str,
    model_title: str,
    max_images_text: str,
) -> None:
    async def fake_upload_message_media_item(message):
        return {
            "type": "photo",
            "file_id": "legacy-photo-id",
            "local_path": "/tmp/legacy-photo-id.png",
            "public_url": "https://example.com/legacy-photo-id.png",
        }

    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)

    async with session_factory() as session:
        await create_user(session, user_id=479, balance=100)
        state = FakeState(
            {
                "model_key": model_key,
                "model_generation_type": "image_edit",
                "model_title": model_title,
                "model_endpoint": f"/api/v3/{model_key}",
                "user_settings": {},
            }
        )
        state.state = GenerationStates.waiting_for_images
        upload_message = FakeMessage(chat_id=479)
        upload_message.photo = [SimpleNamespace(file_id="legacy-photo-id")]

        await generations.process_generation_images(upload_message, state)

        assert t("generation.images_uploaded_progress", "ru", count=1, max_count=10) in upload_message.answers[-1]

        continue_message = FakeMessage(chat_id=479)
        continue_message.text = "✅ Продолжить"
        await generations.continue_after_multi_image_upload(continue_message, state)

        assert state.state == GenerationStates.waiting_for_prompt

        prompt_message = FakeMessage(chat_id=479)
        prompt_message.text = "Make this legacy model flow vivid and clean"
        await generations.process_prompt(prompt_message, state, session)

        assert state.data["prompt"] == "Make this legacy model flow vivid and clean"
        assert model_title in prompt_message.answers[-1]
        assert "legacy-photo-id.png" in str(state.data["input_media_items"])


@pytest.mark.asyncio
async def test_multi_image_upload_survives_back_to_settings_and_appends_from_current_state(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        file_id = message.photo[-1].file_id
        return {
            "type": "photo",
            "file_id": file_id,
            "local_path": f"/tmp/{file_id}.png",
            "public_url": f"https://example.com/{file_id}.png",
        }

    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)

    state = FakeState(
        {
            "model_key": "seedream",
            "model_generation_type": "image_edit",
            "model_title": "Bytedance Seedream V4.5 Edit",
            "model_endpoint": "/api/v3/seedream-v4.5/edit",
            "user_settings": {"size": "1024*1024", "num_generations": "1"},
        }
    )
    state.state = GenerationStates.waiting_for_images

    for index in range(1, 4):
        upload_message = FakeMessage(chat_id=480)
        upload_message.photo = [SimpleNamespace(file_id=f"photo-{index}")]
        await generations.process_generation_images(upload_message, state)

    assert len(state.data["input_media_urls"]) == 3
    assert t("generation.images_uploaded_progress", "ru", count=3, max_count=10) in upload_message.answers[-1]

    back_message = FakeMessage(chat_id=480)
    back_message.text = "⬅️ Назад к настройкам"
    await generations.back_to_settings_from_input_step(back_message, state)

    assert state.state == GenerationStates.choosing_settings
    assert len(state.data["input_media_urls"]) == 3

    continue_message = FakeMessage(chat_id=480)
    callback = FakeCallback(user_id=480, message=continue_message, data="gen:continue")
    await generations.continue_after_settings(callback, state)

    assert state.state == GenerationStates.waiting_for_images
    assert t("generation.images_uploaded_progress", "ru", count=3, max_count=10) in continue_message.answers[-1]
    assert continue_message.answer_markups[-1].keyboard[1][0].text == f"🗑 {t('common.clear_images', 'ru')}"

    fourth_message = FakeMessage(chat_id=480)
    fourth_message.photo = [SimpleNamespace(file_id="photo-4")]
    await generations.process_generation_images(fourth_message, state)

    assert len(state.data["input_media_urls"]) == 4
    assert state.data["input_media_urls"][-1] == "https://example.com/photo-4.png"
    assert t("generation.images_uploaded_progress", "ru", count=4, max_count=10) in fourth_message.answers[-1]


async def await_media_group_task(message: FakeMessage) -> None:
    group_key = generations.get_media_group_key(message)
    assert group_key is not None
    task = generations.MEDIA_GROUP_TASKS[group_key]
    await task


@pytest.mark.asyncio
async def test_media_group_album_adds_three_images_with_single_ui_message(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        file_id = message.photo[-1].file_id
        return {
            "type": "photo",
            "file_id": file_id,
            "local_path": f"/tmp/{file_id}.png",
            "public_url": f"https://example.com/{file_id}.png",
        }

    monkeypatch.setattr(generations, "MEDIA_GROUP_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)
    generations.MEDIA_GROUP_BUFFERS.clear()
    generations.MEDIA_GROUP_STATES.clear()
    generations.MEDIA_GROUP_TASKS.clear()
    generations.MEDIA_GROUP_MODES.clear()

    state = FakeState({"model_key": "seedream", "model_generation_type": "image_edit"})
    messages = []
    for index in range(1, 4):
        message = FakeMessage(chat_id=482)
        message.media_group_id = "album-3"
        message.photo = [SimpleNamespace(file_id=f"album-photo-{index}")]
        messages.append(message)
        await generations.process_generation_images(message, state)

    await await_media_group_task(messages[-1])

    assert state.data["input_media_urls"] == [
        "https://example.com/album-photo-1.png",
        "https://example.com/album-photo-2.png",
        "https://example.com/album-photo-3.png",
    ]
    assert sum(len(message.answers) for message in messages) == 1
    assert messages[0].answers[-1] == t("generation.images_uploaded_progress", "ru", count=3, max_count=10)


@pytest.mark.asyncio
async def test_media_group_album_respects_max_images(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        file_id = message.photo[-1].file_id
        return {
            "type": "photo",
            "file_id": file_id,
            "local_path": f"/tmp/{file_id}.png",
            "public_url": f"https://example.com/{file_id}.png",
        }

    monkeypatch.setattr(generations, "MEDIA_GROUP_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)
    generations.MEDIA_GROUP_BUFFERS.clear()
    generations.MEDIA_GROUP_STATES.clear()
    generations.MEDIA_GROUP_TASKS.clear()
    generations.MEDIA_GROUP_MODES.clear()

    existing_urls = [f"https://example.com/existing-{index}.png" for index in range(8)]
    existing_paths = [f"/tmp/existing-{index}.png" for index in range(8)]
    existing_file_ids = [f"existing-{index}" for index in range(8)]
    state = FakeState(
        {
            "model_key": "seedream",
            "model_generation_type": "image_edit",
            "input_media_urls": existing_urls,
            "input_media_paths": existing_paths,
            "input_media_file_ids": existing_file_ids,
        }
    )
    messages = []
    for index in range(1, 6):
        message = FakeMessage(chat_id=483)
        message.media_group_id = "album-limit"
        message.photo = [SimpleNamespace(file_id=f"limit-photo-{index}")]
        messages.append(message)
        await generations.process_generation_images(message, state)

    await await_media_group_task(messages[-1])

    assert len(state.data["input_media_urls"]) == 10
    assert state.data["input_media_urls"][-2:] == [
        "https://example.com/limit-photo-1.png",
        "https://example.com/limit-photo-2.png",
    ]
    assert sum(len(message.answers) for message in messages) == 1
    assert messages[0].answers[-1] == t("generation.image_limit_reached", "ru", count=10)


@pytest.mark.asyncio
async def test_back_to_settings_preserves_media_group_images(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        file_id = message.photo[-1].file_id
        return {
            "type": "photo",
            "file_id": file_id,
            "local_path": f"/tmp/{file_id}.png",
            "public_url": f"https://example.com/{file_id}.png",
        }

    monkeypatch.setattr(generations, "MEDIA_GROUP_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)
    generations.MEDIA_GROUP_BUFFERS.clear()
    generations.MEDIA_GROUP_STATES.clear()
    generations.MEDIA_GROUP_TASKS.clear()
    generations.MEDIA_GROUP_MODES.clear()

    state = FakeState(
        {
            "model_key": "seedream",
            "model_title": "Bytedance Seedream V4.5 Edit",
            "model_endpoint": "/api/v3/seedream-v4.5/edit",
            "model_generation_type": "image_edit",
            "user_settings": {"size": "1024*1024", "num_generations": "1"},
        }
    )
    state.state = GenerationStates.waiting_for_images
    last_message = None
    for index in range(1, 4):
        last_message = FakeMessage(chat_id=484)
        last_message.media_group_id = "album-back"
        last_message.photo = [SimpleNamespace(file_id=f"back-photo-{index}")]
        await generations.process_generation_images(last_message, state)
    assert last_message is not None
    await await_media_group_task(last_message)

    back_message = FakeMessage(chat_id=484)
    back_message.text = "⬅️ Назад к настройкам"
    await generations.back_to_settings_from_input_step(back_message, state)

    assert state.state == GenerationStates.choosing_settings
    assert len(state.data["input_media_urls"]) == 3


@pytest.mark.asyncio
async def test_single_image_media_group_uses_first_image_only(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        file_id = message.photo[-1].file_id
        return {
            "type": "photo",
            "file_id": file_id,
            "local_path": f"/tmp/{file_id}.png",
            "public_url": f"https://example.com/{file_id}.png",
        }

    monkeypatch.setattr(generations, "MEDIA_GROUP_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)
    generations.MEDIA_GROUP_BUFFERS.clear()
    generations.MEDIA_GROUP_STATES.clear()
    generations.MEDIA_GROUP_TASKS.clear()
    generations.MEDIA_GROUP_MODES.clear()

    state = FakeState({"model_key": "alibaba_wan_2_6_image_to_video_pro", "model_generation_type": "image_to_video"})
    messages = []
    for index in range(1, 4):
        message = FakeMessage(chat_id=485)
        message.media_group_id = "album-single"
        message.photo = [SimpleNamespace(file_id=f"single-photo-{index}")]
        messages.append(message)
        await generations.process_generation_image(message, state)

    await await_media_group_task(messages[-1])

    assert state.state == GenerationStates.waiting_for_prompt
    assert state.data["input_media_urls"] == ["https://example.com/single-photo-1.png"]
    assert state.data["input_media_file_ids"] == ["single-photo-1"]
    assert any(t("generation.single_image_album_first_used", "ru") in answer for answer in messages[0].answers)


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

        assert message.answers[-1] == t("error_ux.invalid_input", "ru")


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
    assert message.answers[-1] == t("generation.flow.lipsync.second", "ru")


@pytest.mark.asyncio
async def test_process_generation_image_for_upscaler_skips_prompt(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        return {
            "type": "photo",
            "file_id": "upscale-photo-id",
            "local_path": "/tmp/upscale-photo-id.png",
            "public_url": "https://example.com/upscale-photo-id.png",
        }

    confirmation_calls = []

    async def fake_show_confirmation_if_media_completes_model(message, state, model):
        confirmation_calls.append(model.key)
        await state.set_state(GenerationStates.waiting_for_confirmation)
        await message.answer("confirmation")
        return True

    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)
    monkeypatch.setattr(generations, "show_confirmation_if_media_completes_model", fake_show_confirmation_if_media_completes_model)

    state = FakeState({"model_key": "wan_ai_image_upscaler", "model_generation_type": "image_to_image"})
    message = FakeMessage(chat_id=409)
    message.photo = [SimpleNamespace(file_id="upscale-photo-id")]

    await generations.process_generation_image(message, state)

    assert confirmation_calls == ["wan_ai_image_upscaler"]
    assert state.state == GenerationStates.waiting_for_confirmation
    assert state.data["input_media_urls"] == ["https://example.com/upscale-photo-id.png"]
    assert message.answers == ["confirmation"]

@pytest.mark.asyncio
async def test_process_generation_image_rejects_video_for_image_flow() -> None:
    state = FakeState({"model_generation_type": "image_edit"})
    message = FakeMessage(chat_id=472)
    message.video = SimpleNamespace(file_id="video-file-id")

    await generations.process_generation_image(message, state)

    assert message.answers[-1] == t("generation.invalid_wait_image", "ru")

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
async def test_process_generation_video_for_lipsync_requests_audio(monkeypatch) -> None:
    async def fake_upload_message_media_item(message):
        return {
            "type": "video",
            "file_id": "video-file-id",
            "local_path": "/tmp/video-file-id.mp4",
            "public_url": "https://example.com/video-file-id.mp4",
        }

    monkeypatch.setattr(generations, "upload_message_media_item", fake_upload_message_media_item)

    state = FakeState(
        {
            "model_key": "kwaivgi_kling_lipsync_audio_to_video",
            "model_generation_type": "lipsync",
            "user_settings": {},
        }
    )
    message = FakeMessage(chat_id=476)
    message.video = SimpleNamespace(file_id="video-file-id")

    await generations.process_generation_video(message, state)

    assert state.state == GenerationStates.waiting_for_audio
    assert state.data["input_media_items"][0]["public_url"] == "https://example.com/video-file-id.mp4"
    assert message.answers[-1] == t("generation.send_audio_for_lipsync", "ru")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute_name", "media_object"),
    [
        ("audio", SimpleNamespace(file_id="audio-file-id", file_size=1024)),
        ("document", SimpleNamespace(file_id="audio-doc-id", mime_type="audio/mpeg", file_size=1024)),
    ],
)
async def test_waiting_for_audio_accepts_audio_inputs(session_factory, monkeypatch, tmp_path, attribute_name, media_object) -> None:
    class FakeTelegramFilesService:
        def __init__(self, bot):
            self.bot = bot

        async def download_temp_file_and_get_public_url(self, file_id: str):
            return SimpleNamespace(local_path=tmp_path / f"{file_id}.mp3", public_url=f"https://example.com/{file_id}.mp3")

    monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)

    async with session_factory() as session:
        await create_user(session, user_id=477, balance=30)
        state = FakeState(
            {
                "model_key": "kwaivgi_kling_lipsync_audio_to_video",
                "model_generation_type": "lipsync",
                "model_title": "Kwaivgi Kling Lipsync Audio To Video",
                "model_endpoint": "https://api.wavespeed.ai/api/v3/kwaivgi/kwaivgi-kling-lipsync-audio-to-video",
                "input_media": {"type": "video", "count": 1},
                "input_media_items": [
                    {
                        "type": "video",
                        "file_id": "video-file-id",
                        "local_path": str(tmp_path / "video.mp4"),
                        "public_url": "https://example.com/video.mp4",
                    }
                ],
                "user_settings": {},
            }
        )
        message = FakeMessage(chat_id=477)
        setattr(message, attribute_name, media_object)

        await generations.process_generation_audio(message, state, session)

        assert state.state == GenerationStates.waiting_for_confirmation
        assert state.data["input_audio_url"].endswith(".mp3")
        assert state.data["input_audio_file_id"] == media_object.file_id
        assert t("generation.voiceover_label", "ru", prompt=t("generation.audio_file", "ru")) in message.answers[-1]


@pytest.mark.asyncio
async def test_waiting_for_audio_rejects_audio_larger_than_docs_limit(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState(
            {
                "model_key": "kwaivgi_kling_lipsync_audio_to_video",
                "model_generation_type": "lipsync",
                "user_settings": {},
            }
        )
        message = FakeMessage(chat_id=478)
        message.audio = SimpleNamespace(file_id="too-large-audio", file_size=(5 * 1024 * 1024) + 1)

        await generations.process_generation_audio(message, state, session)

        assert state.state is None
        assert message.answers[-1] == t("generation.audio_too_large", "ru")


@pytest.mark.asyncio
async def test_waiting_for_audio_accepts_audio_document_by_extension(session_factory, monkeypatch, tmp_path) -> None:
    class FakeTelegramFilesService:
        def __init__(self, bot):
            self.bot = bot

        async def download_temp_file_and_get_public_url(self, file_id: str):
            return SimpleNamespace(local_path=tmp_path / f"{file_id}.mp3", public_url=f"https://example.com/{file_id}.mp3")

    monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)

    async with session_factory() as session:
        await create_user(session, user_id=479, balance=30)
        state = FakeState(
            {
                "model_key": "kwaivgi_kling_lipsync_audio_to_video",
                "model_generation_type": "lipsync",
                "model_title": "Kwaivgi Kling Lipsync Audio To Video",
                "model_endpoint": "https://api.wavespeed.ai/api/v3/kwaivgi/kwaivgi-kling-lipsync-audio-to-video",
                "input_media": {"type": "video", "count": 1},
                "input_media_items": [
                    {
                        "type": "video",
                        "file_id": "video-file-id",
                        "local_path": str(tmp_path / "video.mp4"),
                        "public_url": "https://example.com/video.mp4",
                    }
                ],
                "user_settings": {},
            }
        )
        message = FakeMessage(chat_id=479)
        message.document = SimpleNamespace(file_id="audio-doc-id", mime_type="application/octet-stream", file_name="voice.mp3", file_size=1024)

        await generations.process_generation_audio(message, state, session)

        assert state.state == GenerationStates.waiting_for_confirmation
        assert state.data["input_audio_url"] == "https://example.com/audio-doc-id.mp3"


@pytest.mark.asyncio
async def test_waiting_for_audio_rejects_text_with_audio_file_error(session_factory) -> None:
    async with session_factory() as session:
        state = FakeState({"model_key": "kwaivgi_kling_lipsync_audio_to_video", "user_settings": {}})
        message = FakeMessage(chat_id=480)
        message.text = "not an audio file"

        await generations.invalid_generation_audio(message, state)

        assert message.answers[-1] == t("generation.unsupported_audio_type", "ru")


@pytest.mark.asyncio
async def test_waiting_for_audio_uses_default_20mb_limit_without_docs_max(session_factory) -> None:
    install_audio_no_limit_model()
    try:
        async with session_factory() as session:
            state = FakeState({"model_key": AUDIO_NO_LIMIT_MODEL_KEY, "user_settings": {}})
            message = FakeMessage(chat_id=481)
            message.audio = SimpleNamespace(file_id="too-large-audio", file_size=(20 * 1024 * 1024) + 1)

            await generations.process_generation_audio(message, state, session)

            assert state.state is None
            assert message.answers[-1] == t("generation.audio_too_large", "ru")
    finally:
        remove_audio_no_limit_model()


def test_raw_english_docs_description_is_not_displayed_for_ru_setting_text() -> None:
    model = MODEL_REGISTRY["alibaba_wan_2_6_image_to_video_flash"]

    text = generations.build_setting_value_text(model, "duration", "5", "ru")

    assert "The duration of the generated media" not in text
    assert t("settings.enter_number_value", "ru") in text


def test_settings_screen_shows_generated_or_fallback_settings_without_media_fields() -> None:
    wan = MODEL_REGISTRY["alibaba_wan_2_6_image_to_video_flash"]
    kling = MODEL_REGISTRY["kwaivgi_kling_lipsync_audio_to_video"]
    veo_extend = MODEL_REGISTRY["google_veo3_1_fast_video_extend"]

    wan_text = generations.build_settings_text(wan, {}, "ru")
    kling_text = generations.build_settings_text(kling, {}, "ru")
    veo_text = generations.build_settings_text(veo_extend, {}, "ru")

    assert set(wan.user_settings) - {"num_generations"}
    assert set(veo_extend.user_settings) - {"num_generations"}
    assert "Audio:" not in kling_text
    assert "Аудио:" not in kling_text
    assert "Длительность" in wan_text or "Разрешение" in wan_text
    assert "Разрешение" in veo_text or "Режим" in veo_text or "Качество" in veo_text
    assert "The duration of the generated media" not in wan_text


def test_describe_model_requirements_for_lipsync_audio_model() -> None:
    text = generations.describe_model_requirements(MODEL_REGISTRY["kwaivgi_kling_lipsync_audio_to_video"], "ru")

    assert text == (
        f"<b>{t('requirements.title', 'ru')}</b>\n\n"
        f"• {t('requirements.video_with_face', 'ru')}\n"
        f"• {t('requirements.audio', 'ru')}"
    )


def test_describe_model_requirements_for_text_to_video_model() -> None:
    text = generations.describe_model_requirements(MODEL_REGISTRY["google_veo3"], "ru")

    assert f"• {t('requirements.prompt', 'ru')}" in text
    assert t("requirements.image", "ru") not in text
    assert "Видео" not in text
    assert t("requirements.audio", "ru") not in text


def test_describe_model_requirements_for_image_to_video_model() -> None:
    text = generations.describe_model_requirements(MODEL_REGISTRY["alibaba_wan_2_6_image_to_video_flash"], "ru")

    assert f"• {t('requirements.image', 'ru')}" in text
    assert f"• {t('requirements.prompt', 'ru')}" in text


def test_describe_model_requirements_for_video_extend_model() -> None:
    text = generations.describe_model_requirements(MODEL_REGISTRY["google_veo3_1_fast_video_extend"], "ru")

    assert f"• {t('requirements.video', 'ru')}" in text
    assert f"• {t('requirements.prompt', 'ru')}" in text


def test_describe_model_requirements_translated_ru_and_en_keys() -> None:
    model = MODEL_REGISTRY["alibaba_wan_2_7_reference_to_video"]

    ru_text = generations.describe_model_requirements(model, "ru")
    en_text = generations.describe_model_requirements(model, "en")

    assert "📥 Требуется:" in ru_text
    assert f"• {t('requirements.reference_images', 'ru')}" in ru_text
    assert f"• {t('requirements.prompt', 'ru')}" in ru_text
    assert "📥 Required:" in en_text
    assert f"• {t('requirements.reference_images', 'en')}" in en_text
    assert f"• {t('requirements.prompt', 'en')}" in en_text


def test_insufficient_balance_message_is_localized() -> None:
    assert generations.build_insufficient_balance_message("ru") == t("error_ux.insufficient_balance", "ru")
    assert generations.build_insufficient_balance_message("en") == t("error_ux.insufficient_balance", "en")


def test_insufficient_balance_keyboard_contains_topup_and_profile_buttons() -> None:
    keyboard = generations.build_insufficient_balance_keyboard("ru")

    assert keyboard.inline_keyboard[0][0].text == t("error_ux.button.top_up", "ru")
    assert keyboard.inline_keyboard[0][0].callback_data == "profile:topup"
    assert keyboard.inline_keyboard[1][0].text == t("error_ux.button.profile", "ru")
    assert keyboard.inline_keyboard[1][0].callback_data == "profile:open"


def test_generation_entrypoints_use_shared_insufficient_balance_helper() -> None:
    prompt_source = inspect.getsource(generations.process_prompt)
    confirm_source = inspect.getsource(generations.confirm_generation)

    assert "answer_insufficient_balance" in prompt_source
    assert "answer_insufficient_balance" in confirm_source
    assert "errors.insufficient_balance_details" not in prompt_source
    assert "errors.insufficient_balance_details" not in confirm_source


def test_settings_screen_inserts_requirements_before_current_values() -> None:
    model = MODEL_REGISTRY["alibaba_wan_2_6_image_to_video_flash"]

    text = generations.build_settings_text(model, {}, "ru")

    current_settings = t("settings.current_values", "ru", values="")
    assert text.index(t("requirements.title", "ru")) < text.index(current_settings.split("\n\n", 1)[0])

@pytest.mark.asyncio
async def test_process_generation_video_rejects_photo_for_video_flow() -> None:
    state = FakeState({"model_generation_type": "video_edit"})
    message = FakeMessage(chat_id=474)
    message.photo = [SimpleNamespace(file_id="photo-file-id")]

    await generations.process_generation_video(message, state)

    assert message.answers[-1] == t("generation.invalid_wait_video", "ru")

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

        assert message.answers[-1] == t("error_ux.prompt_required", "ru")


@pytest.mark.asyncio
async def test_process_prompt_saves_lipsync_text_input(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=409, balance=30)
        state = FakeState(
            {
                "model_key": "kwaivgi_kling_lipsync_text_to_video",
                "model_generation_type": "lipsync",
                "model_title": "Lip Model",
                "input_media": {"type": "video", "file_id": "video-file-id"},
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
async def test_waiting_for_audio_accepts_voice_input(session_factory, monkeypatch, tmp_path) -> None:
    class FakeTelegramFilesService:
        def __init__(self, bot):
            self.bot = bot

        async def download_temp_file_and_get_public_url(self, file_id: str):
            return SimpleNamespace(local_path=tmp_path / f"{file_id}.ogg", public_url=f"https://example.com/{file_id}.ogg")

    monkeypatch.setattr(generations, "TelegramFilesService", FakeTelegramFilesService)

    async with session_factory() as session:
        await create_user(session, user_id=410, balance=30)
        state = FakeState(
            {
                "model_key": "kwaivgi_kling_lipsync_audio_to_video",
                "model_generation_type": "lipsync",
                "model_title": "Kwaivgi Kling Lipsync Audio To Video",
                "model_endpoint": "https://api.wavespeed.ai/api/v3/kwaivgi/kwaivgi-kling-lipsync-audio-to-video",
                "input_media": {"type": "video", "count": 1},
                "input_media_items": [
                    {
                        "type": "video",
                        "file_id": "video-file-id",
                        "local_path": str(tmp_path / "video.mp4"),
                        "public_url": "https://example.com/video.mp4",
                    }
                ],
                "user_settings": {},
            }
        )
        message = FakeMessage(chat_id=410)
        message.voice = SimpleNamespace(file_id="voice-file-id", file_size=1024)

        await generations.process_generation_audio(message, state, session)

        assert state.state == GenerationStates.waiting_for_confirmation
        assert state.data["input_audio_url"] == "https://example.com/voice-file-id.ogg"
        assert state.data["input_audio_or_text"]["type"] == "voice"


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
        assert message.answers == [generations.build_insufficient_balance_message("ru")]
        assert message.answer_markups[0].inline_keyboard[0][0].callback_data == "profile:topup"
        assert message.answer_markups[0].inline_keyboard[1][0].callback_data == "profile:open"
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "insufficient_balance"
            and record.msg.get("user_id") == 411
            and record.msg.get("state") == GenerationStates.waiting_for_prompt.state
            and record.msg.get("model_key") == "alibaba_wan_2_6_text_to_image"
            and record.msg.get("total_cost") == 24
            for record in caplog.records
        )
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "generation_insufficient_balance"
            and record.msg.get("user_id") == 411
            and record.msg.get("balance") == 0
            and record.msg.get("required_balance") == 24
            and record.msg.get("model_key") == "alibaba_wan_2_6_text_to_image"
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
    assert state.data["input_media"] == {"type": "image", "count": 1}
    assert state.data["input_media_items"] == []
    assert state.data["input_image_file_id"] == "photo-file-id"
    assert state.data["input_media_urls"] == ["https://example.com/input.png"]
    assert state.data["input_media_paths"] == ["/tmp/input.png"]
    assert state.data["prompt"] is None
    assert state.data["selected_model_key"] == "alibaba_wan_2_6_text_to_image"
    assert state.data["selected_settings"] == {}
    assert message.answer_markups[0].keyboard[0][0].text == "🎨 Генерации"
    assert message.answers[0] == t("generation.back_to_model_settings", "ru")
    assert message.answers[1].startswith(t("generation.settings_header", "ru", model="Alibaba Wan 2.6 Text To Image").split("<b>")[0])
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
async def test_clear_uploaded_images_clears_urls_and_deletes_temp_paths(tmp_path) -> None:
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    state = FakeState(
        {
            "model_key": "seedream",
            "model_generation_type": "image_edit",
            "input_media": {"type": "images", "count": 2},
            "input_media_urls": ["https://example.com/first.png", "https://example.com/second.png"],
            "input_media_paths": [str(first_path), str(second_path)],
            "input_media_file_ids": ["first", "second"],
            "input_media_items": [],
            "input_image_file_id": "first",
        }
    )
    state.state = GenerationStates.waiting_for_images
    message = FakeMessage(chat_id=481)
    message.text = "🗑 Очистить изображения"

    await generations.clear_uploaded_images(message, state)

    assert state.state == GenerationStates.waiting_for_images
    assert state.data["input_media"] is None
    assert state.data["input_media_urls"] == []
    assert state.data["input_media_paths"] == []
    assert state.data["input_media_file_ids"] == []
    assert state.data["input_media_items"] == []
    assert state.data["input_image_file_id"] is None
    assert not first_path.exists()
    assert not second_path.exists()
    assert message.answers[-1] == t("generation.images_cleared", "ru")


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
    assert message.answers[0] == t("generation.back_to_sections", "ru")
    assert f"{t('generation.choose_type', 'ru')}:" in message.answers[1]


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
        await create_user(session, user_id=101, balance=30)

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
            assert await get_user_balance(session, 101) == 13
        finally:
            generations.BACKGROUND_GENERATIONS.clear()
            Path(temp_input_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_confirm_generation_reuses_uploaded_multi_image_items(session_factory, monkeypatch, tmp_path) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=102, balance=30)

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
            }
            assert captured["prediction_id"] == "pred-102"
            assert captured["temp_input_path"] == [str(first_path), str(second_path)]
            assert await get_user_balance(session, 102) == 13
        finally:
            generations.BACKGROUND_GENERATIONS.clear()
            first_path.unlink(missing_ok=True)
            second_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_confirm_generation_keeps_temp_media_until_polling_finishes(session_factory, monkeypatch, tmp_path) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=103, balance=30)

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
        await create_user(session, user_id=104, balance=30)

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
            assert f"{t('generation.choose_type', 'ru')}:" in message.answers[-1]
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

    assert bot.messages[-1] == t("error_ux.delivery_failed", "ru")


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

    assert bot.messages[-1] == t("error_ux.delivery_failed", "ru")
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
        second_generation = await GenerationRepository(session).create_generation_request(
            user_id=307,
            chat_id=777307,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt 2",
            settings={},
            status="processing",
            wavespeed_prediction_id="pred-recover-2",
            cost=2,
        )

    captured = []

    async def fake_poll_generation_result(**kwargs) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(generations, "poll_generation_result", fake_poll_generation_result)

    try:
        recovered_count = await generations.recover_background_generations(FakeBot())
        await await_background_generation_tasks()
    finally:
        generations.BACKGROUND_GENERATIONS.clear()

    assert recovered_count == 2
    captured_by_prediction = {item["prediction_id"]: item for item in captured}
    assert set(captured_by_prediction) == {"pred-recover", "pred-recover-2"}
    assert captured_by_prediction["pred-recover"]["user_id"] == 307
    assert captured_by_prediction["pred-recover"]["chat_id"] == 777307
    assert captured_by_prediction["pred-recover"]["generation_request_id"] == generation.id
    assert captured_by_prediction["pred-recover"]["cost"] == 1
    assert captured_by_prediction["pred-recover"]["temp_input_path"] is None
    assert captured_by_prediction["pred-recover-2"]["generation_request_id"] == second_generation.id
    assert captured_by_prediction["pred-recover-2"]["cost"] == 2


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
async def test_insufficient_balance_does_not_start_submit(session_factory, monkeypatch, caplog) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=401, balance=0)

        called = False
        submit_called = False

        async def fake_poll_generation_result(**kwargs) -> None:
            nonlocal called
            called = True

        async def fake_submit_generation_request(**kwargs) -> str:
            nonlocal submit_called
            submit_called = True
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

        with caplog.at_level(logging.INFO):
            await generations.confirm_generation(callback, state, session)

        assert called is False
        assert submit_called is False
        assert callback.message.answers[-1] == generations.build_insufficient_balance_message("ru")
        assert callback.message.answer_markups[-1].inline_keyboard[0][0].callback_data == "profile:topup"
        assert callback.message.answer_markups[-1].inline_keyboard[1][0].callback_data == "profile:open"
        assert await get_user_balance(session, 401) == 0
        assert await get_generation_count(session, 401) == 0
        assert any(
            isinstance(record.msg, dict)
            and record.msg.get("action") == "generation_insufficient_balance"
            and record.msg.get("user_id") == 401
            and record.msg.get("balance") == 0
            and record.msg.get("required_balance") == 51
            and record.msg.get("model_key") == "nano_banana"
            for record in caplog.records
        )


def test_build_confirmation_text_shows_num_generations_and_total_cost() -> None:
    model = generations.get_generation_model("nano_banana")

    text = generations.build_confirmation_text(
        model,
        {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "3"},
        "Generate three variants",
        balance=100,
    )

    assert t("generation.count_label", "en", count=3) in text
    assert t("generation.cost_label", "en", cost=51) in text
    assert t("generation.balance_after_label", "en", balance=49) in text


def test_generation_started_message_includes_model_and_quantity() -> None:
    text = t("generation.started_count", "en", model="Kling Pro", count=2)

    assert "Generation started" in text
    assert "Model: Kling Pro" in text
    assert "Quantity: 2" in text
    assert "Results will appear here automatically." in text


def test_generation_started_message_is_localized_ru() -> None:
    text = t("generation.started_count", "ru", model="Kling Pro", count=2)

    assert "Генерация запущена" in text
    assert "Модель: Kling Pro" in text
    assert "Количество: 2" in text
    assert "Results will appear" not in text


def test_build_generation_summary_message_formats_visible_settings_and_escapes_html() -> None:
    model = GenerationModel(
        key="summary_test_model",
        title="Kling <Pro>",
        provider="kling",
        generation_type="image_to_video",
        endpoint="/api/v3/kling/test",
        docs_url="https://example.com/docs",
        description="Summary test model",
        max_images=1,
        requires_prompt=True,
        requires_image=True,
        requires_video=False,
        requires_audio=False,
        outputs="video",
        user_settings={
            "num_generations": GenerationSetting(
                key="num_generations",
                title="Generation count",
                type="select",
                default="1",
                options=(SettingOption(value="4", label="4"),),
            ),
            "resolution": GenerationSetting(
                key="resolution",
                title="Resolution",
                type="select",
                default="720p",
                options=(SettingOption(value="1080p", label="1080p"),),
            ),
            "internal_token": GenerationSetting(
                key="internal_token",
                title="Internal token",
                type="text",
                default="secret",
                options=(),
                is_user_visible=False,
            ),
        },
    )
    batch = generations.GenerationBatchSummary(
        model=model,
        prompt="Render <b>cinematic</b> shot",
        settings={"num_generations": "4", "resolution": "1080p", "internal_token": "raw-api-key"},
        expected_count=4,
        completed_count=4,
        failed_count=0,
        credits_spent=24,
    )

    text = generations.build_generation_summary_message(batch, "en")

    assert t("generation.summary.title", "en") in text
    assert t("generation.summary.model", "en", model="Kling &lt;Pro&gt;") in text
    assert t("generation.summary.type", "en", generation_type="Image to Video") in text
    assert "Prompt:" in text
    assert "Settings:" in text
    assert "Render &lt;b&gt;cinematic&lt;/b&gt; shot" in text
    assert f"• {t('settings.title.num_generations', 'en')}: <code>4</code>" in text
    assert "• Resolution: <code>1080p</code>" in text
    assert "internal_token" not in text
    assert "raw-api-key" not in text
    assert t("generation.summary.results", "en", completed=4, expected=4) in text
    assert t("generation.summary.credits", "en", credits=24) in text


def test_setting_input_screen_explains_current_value_and_clear_hint() -> None:
    model = GenerationModel(
        key="setting_input_test_model",
        title="Settings Test",
        provider="wavespeed",
        generation_type="text_to_image",
        endpoint="/api/v3/test/settings",
        docs_url="https://example.com/docs",
        description="Settings test model",
        max_images=0,
        requires_prompt=True,
        requires_image=False,
        requires_video=False,
        requires_audio=False,
        outputs="image",
        user_settings={
            "negative_prompt": GenerationSetting(
                key="negative_prompt",
                title="Negative prompt",
                type="text",
                default="",
                options=(),
            ),
        },
    )
    text = generations.build_setting_value_text(model, "negative_prompt", "", "en")

    assert "⚙️ Setting:" in text
    assert "Current:" in text
    assert "Send a new value in the next message." in text
    assert "Send <code>-</code> to clear it." in text
    assert "Choose a value" not in text


def test_build_generation_summary_message_localizes_empty_prompt_and_partial_failure() -> None:
    model = generations.get_generation_model("nano_banana")
    batch = generations.GenerationBatchSummary(
        model=model,
        prompt="",
        settings={"num_generations": "10", "resolution": "4k", "aspect_ratio": "1:1", "output_format": "png"},
        expected_count=10,
        completed_count=7,
        failed_count=3,
        credits_spent=119,
    )

    text = generations.build_generation_summary_message(batch, "ru")

    assert t("generation.summary.title", "ru") in text
    assert t("generation.summary.no_prompt", "ru") in text
    assert f"• {t('settings.title.num_generations', 'ru')}: <code>10</code>" in text
    assert t("generation.summary.results", "ru", completed=7, expected=10) in text
    assert t("generation.summary.partial_failed", "ru", count=3) in text
    assert t("generation.summary.refund_done", "ru") in text
    assert t("generation.summary.credits", "ru", credits=119) in text


def test_build_generation_summary_message_truncates_long_prompt() -> None:
    model = generations.get_generation_model("nano_banana")
    long_prompt = f"{'a' * 1600}<secret>"
    batch = generations.GenerationBatchSummary(
        model=model,
        prompt=long_prompt,
        settings={"num_generations": "1", "resolution": "4k", "aspect_ratio": "1:1", "output_format": "png"},
        expected_count=1,
        completed_count=1,
        failed_count=0,
        credits_spent=17,
    )

    text = generations.build_generation_summary_message(batch, "en")

    assert "..." in text
    assert "&lt;secret&gt;" not in text


def test_build_settings_text_shows_price_and_recalculates_duration() -> None:
    model = generations.get_generation_model("alibaba_wan_2_6_text_to_video")

    default_text = generations.build_settings_text(
        model,
        {"size": "1280*720", "duration": "5", "negative_prompt": "", "num_generations": "1"},
        "ru",
    )
    longer_text = generations.build_settings_text(
        model,
        {"size": "1280*720", "duration": "10", "negative_prompt": "", "num_generations": "1"},
        "ru",
    )

    assert "💰 Цена: ≈ 6 credits" in default_text
    assert "(~$0.08)" in default_text
    assert "💰 Цена: ≈ 6 credits" in longer_text
    assert "(~$0.08)" in longer_text


@pytest.mark.asyncio
async def test_confirm_generation_debits_total_cost_and_persists_num_generations(session_factory, monkeypatch) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=451, balance=100)
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
        assert sorted(generation.cost for generation in generation_requests) == [17, 17, 17]
        assert sum(generation.cost for generation in generation_requests) == 51
        assert all(generation.settings["num_generations"] == "3" for generation in generation_requests)
        assert await get_user_balance(session, 451) == 49
        assert len(captured["generation_predictions"]) == 3
        assert len(captured["submit_calls"]) == 3


@pytest.mark.asyncio
async def test_num_generations_ten_starts_ten_submit_requests(session_factory, monkeypatch, tmp_path) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=452, balance=200)

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
                "prompt": "Generate ten variants",
                "input_image_file_id": "telegram-file-id",
                "user_settings": {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png", "num_generations": "10"},
            }
        )
        callback = FakeCallback(user_id=452)

        await generations.confirm_generation(callback, state, session)
        await await_background_generation_tasks()

        assert len(batch_calls["generation_predictions"]) == 10
        assert len(batch_calls["submit_calls"]) == 10
        assert batch_calls["generation_costs"] == {generation_id: 17 for generation_id, _ in batch_calls["generation_predictions"]}
        assert await get_user_balance(session, 452) == 30
        assert callback.message.answers[-1] == t("generation.started", "ru")


def test_parallel_generation_limit_artifacts_are_absent() -> None:
    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (Path("app"), Path("tests"))
        for path in root.rglob("*.py")
    )

    removed_warning = "Можно запускать" + " не больше 3 генераций"
    removed_action = "parallel_generation_" + "limit_reached"
    removed_asyncio_semaphore = "asyncio." + "Sem" + "aphore" + "(" + "4)"
    removed_semaphore = "Sem" + "aphore" + "(" + "4)"
    assert removed_warning not in source_text
    assert removed_action not in source_text
    assert removed_asyncio_semaphore not in source_text
    assert removed_semaphore not in source_text


@pytest.mark.asyncio
async def test_single_generation_sends_summary_once_after_output(session_factory, monkeypatch, tmp_path) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=459, balance=100)
        generation = await GenerationRepository(session).create_generation_request(
            user_id=459,
            model_key="nano_banana",
            model_endpoint="/api/v3/nano-banana",
            prompt="Prompt <safe>",
            settings={"num_generations": "1", "resolution": "4k", "aspect_ratio": "1:1", "output_format": "png"},
            status="created",
            cost=17,
        )

    temp_input_path = tmp_path / "single-input.png"
    temp_input_path.write_bytes(b"input")

    class SuccessfulWavespeedService:
        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            return SimpleNamespace(raw_response={"nsfw_flags": None}, outputs=["https://example.com/single.jpg"])

        async def close(self) -> None:
            return None

    delivery_calls: list[list[str]] = []

    async def fake_send_generation_outputs(*args, **kwargs):
        delivery_calls.append(args[2])
        return generations.OutputDeliveryResult(delivered_successfully=True)

    bot = FakeBot()
    monkeypatch.setattr(generations, "WavespeedService", SuccessfulWavespeedService)
    monkeypatch.setattr(generations, "send_generation_outputs", fake_send_generation_outputs)

    await generations.poll_generation_result(
        bot=bot,
        user_id=459,
        chat_id=459,
        generation_request_id=generation.id,
        prediction_id="pred-single",
        model_key="nano_banana",
        cost=17,
        temp_input_path=str(temp_input_path),
    )

    assert len(delivery_calls) == 1
    assert len(bot.messages) == 1
    assert t("generation.summary.title", "en") in bot.messages[0]
    assert "Prompt &lt;safe&gt;" in bot.messages[0]
    assert t("generation.summary.results", "en", completed=1, expected=1) in bot.messages[0]
    assert bot.message_parse_modes == ["HTML"]
    assert len(bot.message_markups[0].inline_keyboard) == 1
    assert bot.message_markups[0].inline_keyboard[0][0].callback_data == f"gen:repeat:{generation.id}"
    assert temp_input_path.exists() is False


@pytest.mark.asyncio
async def test_summary_repeat_restores_exact_alibaba_model_and_ignores_current_fsm(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=462, balance=200)
        model = generations.get_generation_model("alibaba_wan_2_7_text_to_image_pro")
        generation = await GenerationRepository(session).create_generation_request(
            user_id=462,
            model_key=model.key,
            model_endpoint=model.endpoint,
            prompt="A cinematic image prompt",
            settings={**generations.get_default_settings(model.key), "num_generations": "2"},
            status="completed",
            cost=34,
        )

        state = FakeState({"model_key": "nano_banana", "user_settings": {"num_generations": "10"}})
        callback = FakeCallback(user_id=462, data=f"gen:repeat:{generation.id}")

        await generations.repeat_generation_from_summary(callback, state, session)

        state_data = await state.get_data()
        assert state_data["model_key"] == "alibaba_wan_2_7_text_to_image_pro"
        assert state_data["model_title"] == model.title
        assert state_data["model_generation_type"] == model.generation_type
        assert state_data["prompt"] == "A cinematic image prompt"
        assert state_data["user_settings"]["num_generations"] == "2"
        assert state.state == GenerationStates.waiting_for_confirmation
        assert callback.message.answers[0] == t("generation.repeat_title", "ru", model=model.title)
        assert any("Alibaba Wan 2.7 Text To Image Pro" in answer for answer in callback.message.answers)


@pytest.mark.asyncio
async def test_summary_repeat_text_only_opens_confirmation(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=463, balance=200)
        model = generations.get_generation_model("alibaba_wan_2_7_text_to_image_pro")
        generation = await GenerationRepository(session).create_generation_request(
            user_id=463,
            model_key=model.key,
            model_endpoint=model.endpoint,
            prompt="A repeatable text only prompt",
            settings={**generations.get_default_settings(model.key), "num_generations": "1"},
            status="completed",
            cost=17,
        )

        state = FakeState()
        callback = FakeCallback(user_id=463, data=f"gen:repeat:{generation.id}")

        await generations.repeat_generation_from_summary(callback, state, session)

        assert state.state == GenerationStates.waiting_for_confirmation
        assert callback.message.answers[0] == t("generation.repeat_title", "ru", model=model.title)
        assert t("generation.review", "ru") in callback.message.answers[-1]
        assert "A repeatable text only prompt" in callback.message.answers[-1]


@pytest.mark.asyncio
async def test_summary_repeat_english_user_does_not_mix_russian(session_factory) -> None:
    async with session_factory() as session:
        user = await create_user(session, user_id=465, balance=200)
        user.language_code = "en"
        await session.commit()
        model = generations.get_generation_model("alibaba_wan_2_7_text_to_image_pro")
        generation = await GenerationRepository(session).create_generation_request(
            user_id=465,
            model_key=model.key,
            model_endpoint=model.endpoint,
            prompt="A repeatable English prompt",
            settings={**generations.get_default_settings(model.key), "num_generations": "1"},
            status="completed",
            cost=17,
        )

        state = FakeState()
        callback = FakeCallback(user_id=465, data=f"gen:repeat:{generation.id}")
        callback.from_user.language_code = "ru"

        await generations.repeat_generation_from_summary(callback, state, session)

        combined_text = "\n".join(callback.message.answers)
        assert callback.message.answers[0] == t("generation.repeat_title", "en", model=model.title)
        assert t("generation.review", "en") in combined_text
        assert "Model:" in combined_text
        assert t("generation.repeat_title", "ru", model=model.title) not in combined_text
        assert t("generation.review", "ru") not in combined_text


@pytest.mark.asyncio
async def test_summary_repeat_media_model_asks_for_media_again(session_factory) -> None:
    async with session_factory() as session:
        await create_user(session, user_id=464, balance=200)
        model = generations.get_generation_model("alibaba_wan_2_7_image_to_video")
        generation = await GenerationRepository(session).create_generation_request(
            user_id=464,
            model_key=model.key,
            model_endpoint=model.endpoint,
            prompt="Animate this image with soft camera motion",
            settings={**generations.get_default_settings(model.key), "num_generations": "1"},
            status="completed",
            cost=17,
        )

        state = FakeState({"model_key": "alibaba_wan_2_7_text_to_image_pro"})
        callback = FakeCallback(user_id=464, data=f"gen:repeat:{generation.id}")

        await generations.repeat_generation_from_summary(callback, state, session)

        state_data = await state.get_data()
        assert state_data["model_key"] == "alibaba_wan_2_7_image_to_video"
        assert state_data["prompt"] == "Animate this image with soft camera motion"
        assert state.state in {GenerationStates.waiting_for_image, GenerationStates.waiting_for_images}
        assert callback.message.answers[0] == t("generation.repeat_title", "ru", model=model.title)
        assert any("Отправьте изображ" in answer for answer in callback.message.answers)


@pytest.mark.asyncio
async def test_ten_generation_batch_sends_one_summary_after_outputs(session_factory, monkeypatch, tmp_path) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=461, balance=200)
        generation_ids = []
        for _ in range(10):
            generation = await GenerationRepository(session).create_generation_request(
                user_id=461,
                model_key="nano_banana",
                model_endpoint="/api/v3/nano-banana",
                prompt="Batch prompt",
                settings={"num_generations": "10", "resolution": "4k", "aspect_ratio": "1:1", "output_format": "png"},
                status="created",
                cost=17,
            )
            generation_ids.append(generation.id)

    temp_input_path = tmp_path / "batch-success-input.png"
    temp_input_path.write_bytes(b"input")
    generation_predictions = [(generation_id, f"pred-success-{index}") for index, generation_id in enumerate(generation_ids)]

    class SuccessfulWavespeedService:
        async def poll_until_complete(self, prediction_id: str, cancel_event=None, timeout_seconds=600, interval=60, **kwargs):
            return SimpleNamespace(raw_response={"nsfw_flags": None}, outputs=[f"https://example.com/{prediction_id}.jpg"])

        async def close(self) -> None:
            return None

    delivery_calls: list[list[str]] = []

    async def fake_send_generation_outputs(*args, **kwargs):
        delivery_calls.append(args[2])
        return generations.OutputDeliveryResult(delivered_successfully=True)

    bot = FakeBot()
    monkeypatch.setattr(generations, "WavespeedService", SuccessfulWavespeedService)
    monkeypatch.setattr(generations, "send_generation_outputs", fake_send_generation_outputs)

    await generations.poll_generation_results_batch(
        bot=bot,
        user_id=461,
        chat_id=461,
        generation_predictions=generation_predictions,
        model_key="nano_banana",
        cost=17,
        temp_input_path=str(temp_input_path),
    )

    assert len(delivery_calls) == 10
    assert len(bot.messages) == 1
    assert t("generation.summary.results", "en", completed=10, expected=10) in bot.messages[0]
    assert t("generation.summary.credits", "en", credits=170) in bot.messages[0]
    assert bot.message_parse_modes == ["HTML"]
    assert temp_input_path.exists() is False


@pytest.mark.asyncio
async def test_batch_failure_refunds_only_one_credit_and_cleans_up_after_all_tasks(session_factory, monkeypatch, tmp_path) -> None:
    session_maker = session_factory
    monkeypatch.setattr(generations.db_manager, "session_factory", session_maker)

    async with session_maker() as session:
        await create_user(session, user_id=460, balance=170)
        generation_ids = []
        for _ in range(10):
            generation = await GenerationRepository(session).create_generation_request(
                user_id=460,
                model_key="nano_banana",
                model_endpoint="/api/v3/nano-banana",
                prompt="Prompt",
                settings={"num_generations": "10"},
                status="created",
                cost=17,
            )
            generation_ids.append(generation.id)
        await UserRepository(session).decrease_balance(460, 170)

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
            if prediction_map[prediction_id] in {1, 4, 8}:
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
        cost=17,
        temp_input_path=str(temp_input_path),
    )

    async with session_maker() as session:
        assert await get_user_balance(session, 460) == 51

    assert len(delivery_calls) == 7
    assert temp_input_path.exists() is False
    assert bot.messages.count(t("error_ux.generation_failed", "en")) == 3
    assert t("generation.summary.results", "en", completed=7, expected=10) in bot.messages[-1]
    assert t("generation.summary.partial_failed", "en", count=3) in bot.messages[-1]
    assert t("generation.summary.refund_done", "en") in bot.messages[-1]
    assert t("generation.summary.credits", "en", credits=119) in bot.messages[-1]


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
    assert bot.messages[-1] == t("download.upload_failed", "en")
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
    assert bot.messages[-1] == t("download.upload_failed", "en")


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
    assert bot.messages[-1] == generations.build_large_file_r2_message(
        "https://example.com/d/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
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
    assert bot.messages[-1] == generations.build_large_file_r2_message(
        "https://example.com/d/retry-fallback-token"
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
    assert bot.messages[-1] == t("download.upload_failed", "en")


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
    assert bot.messages[-1] == t("download.upload_failed", "en")


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
    assert bot.messages[-1] == t("download.telegram_failed", "en")


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
async def test_send_generation_outputs_loads_profile_preference_true_and_sends_png_as_document(session_factory, monkeypatch, tmp_path, caplog) -> None:
    output_path = tmp_path / "imai-photo.png"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/png", 5

    async with session_factory() as session:
        await create_user(session, user_id=503, balance=3)
        await UserRepository(session).set_user_delivery_preference(503, True)

    bot = FakeBot()
    caplog.set_level(logging.INFO, logger="telegram_bot")
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations.db_manager, "session_factory", session_factory)

    delivered = await generations.send_generation_outputs(bot, 503, ["https://example.com/output.png"], user_id=503)

    assert delivered.delivered_successfully is True
    assert bot.documents != []
    assert bot.photos == []
    assert bot.videos == []
    delivery_logs = [record.msg for record in caplog.records if isinstance(record.msg, dict) and record.msg.get("action") == "generation_output_delivery"]
    assert delivery_logs[-1]["send_results_as_files"] is True
    assert delivery_logs[-1]["content_type"] == "image/png"
    assert delivery_logs[-1]["delivery_method"] == "document"


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
async def test_send_generation_outputs_loads_profile_preference_true_and_sends_mp4_as_document(session_factory, monkeypatch, tmp_path, caplog) -> None:
    output_path = tmp_path / "imai-video.mp4"
    output_path.write_bytes(b"video")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "video/mp4", 5

    async with session_factory() as session:
        await create_user(session, user_id=504, balance=3)
        await UserRepository(session).set_user_delivery_preference(504, True)

    bot = FakeBot()
    caplog.set_level(logging.INFO, logger="telegram_bot")
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations.db_manager, "session_factory", session_factory)

    delivered = await generations.send_generation_outputs(bot, 504, ["https://example.com/output.mp4"], user_id=504)

    assert delivered.delivered_successfully is True
    assert bot.documents != []
    assert bot.photos == []
    assert bot.videos == []
    delivery_logs = [record.msg for record in caplog.records if isinstance(record.msg, dict) and record.msg.get("action") == "generation_output_delivery"]
    assert delivery_logs[-1]["send_results_as_files"] is True
    assert delivery_logs[-1]["content_type"] == "video/mp4"
    assert delivery_logs[-1]["delivery_method"] == "document"


@pytest.mark.asyncio
async def test_send_generation_outputs_loads_profile_preference_false_and_sends_png_as_photo(session_factory, monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-photo.png"
    output_path.write_bytes(b"image")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "image/png", 5

    async with session_factory() as session:
        await create_user(session, user_id=505, balance=3)
        await UserRepository(session).set_user_delivery_preference(505, False)

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations.db_manager, "session_factory", session_factory)

    delivered = await generations.send_generation_outputs(bot, 505, ["https://example.com/output.png"], user_id=505)

    assert delivered.delivered_successfully is True
    assert bot.photos != []
    assert bot.documents == []
    assert bot.videos == []


@pytest.mark.asyncio
async def test_send_generation_outputs_loads_profile_preference_false_and_sends_mp4_as_video(session_factory, monkeypatch, tmp_path) -> None:
    output_path = tmp_path / "imai-video.mp4"
    output_path.write_bytes(b"video")

    async def fake_download_output_file_to_temp(output_url: str):
        return str(output_path), "video/mp4", 5

    async with session_factory() as session:
        await create_user(session, user_id=506, balance=3)
        await UserRepository(session).set_user_delivery_preference(506, False)

    bot = FakeBot()
    monkeypatch.setattr(generations, "download_output_file_to_temp", fake_download_output_file_to_temp)
    monkeypatch.setattr(generations.db_manager, "session_factory", session_factory)

    delivered = await generations.send_generation_outputs(bot, 506, ["https://example.com/output.mp4"], user_id=506)

    assert delivered.delivered_successfully is True
    assert bot.videos != []
    assert bot.photos == []
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

    assert bot.messages[-1] == t("error_ux.delivery_failed", "ru")