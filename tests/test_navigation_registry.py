"""Smoke checks for generation navigation registry and keyboards."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.keyboards import (
    build_generation_confirm_keyboard,
    build_generation_sections_keyboard,
    build_model_settings_keyboard,
    build_models_keyboard,
    build_providers_keyboard,
    build_setting_options_keyboard,
)
from app.bot.routers import generations
from app.i18n import t
from app.services.generation_service import (
    GENERATION_TYPES,
    MODEL_REGISTRY,
    PROVIDERS,
    GenerationModel,
    build_payload,
    list_generation_models,
    list_models_by_provider,
    list_models_by_type,
    list_providers,
)


class FakeState:
    def __init__(self, data: dict[str, object] | None = None):
        self.data = data or {}
        self.state = None

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def set_state(self, state) -> None:
        self.state = state


class FakeMessage:
    def __init__(self, chat_id: int = 1):
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=chat_id)
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


class FakeCallback:
    def __init__(self, user_id: int = 1, message: FakeMessage | None = None, data: str = ""):
        self.from_user = SimpleNamespace(id=user_id, language_code="ru")
        self.message = message or FakeMessage(chat_id=user_id)
        self.data = data

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        return None


def _iter_callback_data(markup) -> list[str]:
    callback_data: list[str] = []
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data is not None:
                callback_data.append(button.callback_data)
    return callback_data


def _build_minimal_payload_for_model(model: GenerationModel) -> dict[str, object]:
    image_urls: list[str] = []
    prompt = "Smoke test prompt"
    user_settings: dict[str, str] = {}

    if model.generation_type == "lipsync":
        image_urls = ["https://example.com/face.mp4"]
        user_settings["input_video_url"] = image_urls[0]
        image_urls = []
        if model.requires_audio:
            prompt = ""
            user_settings["input_audio_url"] = "https://example.com/input.mp3"
        else:
            prompt = "Lip sync text"
    elif model.requires_image:
        image_urls = ["https://example.com/input.png"]
    elif model.requires_video:
        image_urls = ["https://example.com/input.mp4"]
    if model.requires_audio:
        user_settings["input_audio_url"] = "https://example.com/input.mp3"

    if not model.requires_prompt and model.generation_type != "lipsync":
        prompt = ""

    return build_payload(model.key, image_urls, prompt, user_settings)


def test_all_generation_sections_return_a_list_without_error() -> None:
    for generation_type in GENERATION_TYPES:
        models = list_models_by_type(generation_type)
        assert isinstance(models, list)


def test_all_list_returns_enabled_providers() -> None:
    assert list_providers() == [
        "alibaba",
        "bytedance",
        "google",
        "openai",
        "kling",
        "grok",
        "minimax",
        "wavespeed_ai",
    ]
    assert all(provider in PROVIDERS for provider in list_providers())


def test_midjourney_is_removed_from_provider_lists_and_keyboard() -> None:
    assert "midjourney" not in list_providers()
    provider_callbacks = _iter_callback_data(build_providers_keyboard())
    assert all(not callback_data.startswith("gen:provider:midjourney") for callback_data in provider_callbacks)


def test_bytedance_provider_appears_in_all_models_keyboard() -> None:
    callbacks = _iter_callback_data(build_providers_keyboard())

    assert "gen:provider:bytedance:0" in callbacks
    assert "gen:provider:kling:0" in callbacks
    assert "gen:provider:grok:0" in callbacks
    assert "gen:provider:minimax:0" in callbacks
    assert "gen:provider:wavespeed_ai:0" in callbacks


def test_new_providers_appear_when_they_have_enabled_models() -> None:
    injected_providers = ["openai", "kling", "grok", "minimax", "wavespeed_ai"]
    for provider in injected_providers:
        MODEL_REGISTRY[f"enabled_{provider}_navigation_model"] = GenerationModel(
            key=f"enabled_{provider}_navigation_model",
            title=f"Enabled {provider} Navigation Model",
            provider=provider,
            generation_type="text_to_image",
            endpoint=f"https://api.wavespeed.ai/api/v3/{provider}/enabled-navigation-model",
            docs_url=f"https://wavespeed.ai/docs/docs-api/{provider}/{provider}-enabled-navigation-model",
            description="Enabled test model",
            max_images=1,
            requires_prompt=True,
            requires_image=False,
            requires_video=False,
            requires_audio=False,
            outputs="image",
            is_enabled=True,
            user_settings={},
        )
    try:
        assert list_providers() == [
            "alibaba",
            "bytedance",
            "google",
            "openai",
            "kling",
            "grok",
            "minimax",
            "wavespeed_ai",
        ]
        callbacks = _iter_callback_data(build_providers_keyboard())
        button_texts = [button.text for row in build_providers_keyboard().inline_keyboard for button in row]
        assert "gen:provider:kling:0" in callbacks
        assert "gen:provider:grok:0" in callbacks
        assert "gen:provider:minimax:0" in callbacks
        assert "gen:provider:wavespeed_ai:0" in callbacks
        assert "MiniMax" in button_texts
        assert "Wan AI" in button_texts
        assert "Wavespeed AI" not in button_texts
    finally:
        for provider in injected_providers:
            MODEL_REGISTRY.pop(f"enabled_{provider}_navigation_model", None)


def test_each_enabled_model_has_required_metadata_and_builds_payload() -> None:
    for model in list_generation_models():
        assert model.provider
        assert model.generation_type
        assert model.endpoint
        assert model.docs_url
        assert model.required_payload_fields
        assert _build_minimal_payload_for_model(model)


def test_enabled_model_payloads_stay_within_allowed_fields() -> None:
    for model in list_generation_models():
        payload = _build_minimal_payload_for_model(model)
        assert set(payload).issubset(set(model.allowed_payload_fields))


def test_callback_data_of_all_navigation_keyboards_fit_telegram_limit() -> None:
    sample_model = next(model for model in list_generation_models() if model.user_settings)
    sample_setting_key = next(iter(sample_model.user_settings))
    sample_current_settings = {
        key: setting.default for key, setting in sample_model.user_settings.items()
    }

    markups = [
        build_generation_sections_keyboard(),
        build_providers_keyboard(),
        build_models_keyboard(list_generation_models(), "gen:back:sections"),
        build_model_settings_keyboard(sample_model, sample_current_settings),
        build_setting_options_keyboard(sample_model, sample_setting_key, sample_current_settings[sample_setting_key]),
        build_generation_confirm_keyboard(),
    ]

    for markup in markups:
        for callback_data in _iter_callback_data(markup):
            assert len(callback_data.encode("utf-8")) < 64


def test_navigation_keyboards_include_back_buttons() -> None:
    sample_model = next(model for model in list_generation_models() if model.user_settings)
    sample_setting_key = next(iter(sample_model.user_settings))
    sample_current_settings = {
        key: setting.default for key, setting in sample_model.user_settings.items()
    }

    sections_callbacks = _iter_callback_data(build_generation_sections_keyboard())
    provider_callbacks = _iter_callback_data(build_providers_keyboard())
    model_callbacks = _iter_callback_data(build_models_keyboard(list_generation_models(), "gen:back:providers"))
    settings_callbacks = _iter_callback_data(build_model_settings_keyboard(sample_model, sample_current_settings))
    option_callbacks = _iter_callback_data(
        build_setting_options_keyboard(sample_model, sample_setting_key, sample_current_settings[sample_setting_key])
    )

    assert "gen:back:main" not in sections_callbacks
    assert "gen:back:sections" in provider_callbacks
    assert "gen:back:providers" in model_callbacks
    assert "gen:back:models" in settings_callbacks
    assert "gen:back:settings" in option_callbacks


def test_disabled_models_are_hidden_from_user_lists() -> None:
    MODEL_REGISTRY["disabled_navigation_model"] = GenerationModel(
        key="disabled_navigation_model",
        title="Disabled Navigation Model",
        provider="google",
        generation_type="text_to_image",
        endpoint="https://api.wavespeed.ai/api/v3/google/disabled-navigation-model",
        docs_url="https://wavespeed.ai/docs/docs-api/google/google-disabled-navigation-model",
        description="Disabled test model",
        max_images=1,
        requires_prompt=True,
        requires_image=False,
        requires_video=False,
        requires_audio=False,
        outputs="image",
        is_enabled=False,
        warning="Endpoint needs verification",
        user_settings={},
    )
    try:
        assert all(model.key != "disabled_navigation_model" for model in list_generation_models())
        assert all(model.key != "disabled_navigation_model" for model in list_models_by_type("text_to_image"))
        assert all(model.key != "disabled_navigation_model" for model in list_models_by_provider("google"))
    finally:
        MODEL_REGISTRY.pop("disabled_navigation_model", None)


def test_categories_show_only_enabled_models() -> None:
    enabled_types = {model.generation_type for model in list_generation_models()}
    section_callbacks = _iter_callback_data(build_generation_sections_keyboard())

    for generation_type in GENERATION_TYPES:
        models = list_models_by_type(generation_type)
        assert all(model.is_enabled for model in models)
        if generation_type == "image_to_image":
            assert models
            assert "gen:section:image_to_image" not in section_callbacks
        elif generation_type == "video_to_audio":
            assert models == []
            assert "gen:section:video_to_audio" not in section_callbacks
        elif generation_type in enabled_types:
            assert models
            assert f"gen:section:{generation_type}" in section_callbacks
        else:
            assert models == []
            assert f"gen:section:{generation_type}" not in section_callbacks


@pytest.mark.asyncio
async def test_ui_shows_placeholder_when_provider_has_no_models(monkeypatch) -> None:
    state = FakeState({"selected_generation_type": "all"})
    message = FakeMessage(chat_id=500)
    callback = FakeCallback(user_id=500, message=message, data="gen:provider:google")

    monkeypatch.setattr(generations, "list_models_by_provider", lambda provider: [])

    await generations.choose_provider(callback, state)

    assert message.edits[-1] == t("generation.no_models_in_provider", "ru")
    callbacks = _iter_callback_data(message.edit_markups[-1])
    assert "gen:back:sections" in callbacks


@pytest.mark.asyncio
async def test_all_models_section_callback_opens_provider_list() -> None:
    state = FakeState()
    message = FakeMessage(chat_id=501)
    callback = FakeCallback(user_id=501, message=message, data="gen:section:all_models")

    await generations.choose_generation_section(callback, state)

    assert state.state == generations.GenerationStates.choosing_provider
    assert state.data["selected_generation_type"] == "all"
    callbacks = _iter_callback_data(message.edit_markups[-1])
    assert "gen:provider:google:0" in callbacks
    assert "gen:back:sections" in callbacks


@pytest.mark.asyncio
async def test_generation_models_page_callback_opens_category_page(monkeypatch) -> None:
    models = [SimpleNamespace(key=f"model_{index}", title=f"Model {index}") for index in range(9)]
    state = FakeState()
    message = FakeMessage(chat_id=502)
    callback = FakeCallback(user_id=502, message=message, data="gen:models:image_edit:1")

    monkeypatch.setattr(generations, "list_models_by_type", lambda generation_type: models)

    await generations.show_generation_models_page(callback, state)

    assert state.state == generations.GenerationStates.choosing_generation_type
    assert state.data["selected_generation_type"] == "image_edit"
    assert state.data["selected_model_page"] == 1
    callbacks = _iter_callback_data(message.edit_markups[-1])
    assert "gen:model:model_8" in callbacks
    assert "gen:models:image_edit:0" in callbacks


@pytest.mark.asyncio
async def test_provider_page_callback_opens_provider_model_page(monkeypatch) -> None:
    models = [SimpleNamespace(key=f"provider_model_{index}", title=f"Provider Model {index}") for index in range(9)]
    state = FakeState({"selected_generation_type": "all"})
    message = FakeMessage(chat_id=503)
    callback = FakeCallback(user_id=503, message=message, data="gen:provider:google:1")

    monkeypatch.setattr(generations, "list_models_by_provider", lambda provider: models)

    await generations.choose_provider(callback, state)

    assert state.state == generations.GenerationStates.choosing_provider
    assert state.data["selected_provider"] == "google"
    assert state.data["selected_model_page"] == 1
    callbacks = _iter_callback_data(message.edit_markups[-1])
    assert "gen:model:provider_model_8" in callbacks
    assert "gen:provider:google:0" in callbacks
