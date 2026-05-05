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
        self.from_user = SimpleNamespace(id=user_id)
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

    if model.generation_type == "lipsync":
        image_urls = ["https://example.com/face.png"]
        prompt = "Lip sync text"
    elif model.requires_image:
        image_urls = ["https://example.com/input.png"]
    elif model.requires_video:
        image_urls = ["https://example.com/input.mp4"]

    if not model.requires_prompt and model.generation_type != "lipsync":
        prompt = ""

    return build_payload(model.key, image_urls, prompt)


def test_all_generation_sections_return_a_list_without_error() -> None:
    for generation_type in GENERATION_TYPES:
        models = list_models_by_type(generation_type)
        assert isinstance(models, list)


def test_all_list_returns_all_providers() -> None:
    assert list_providers() == PROVIDERS


def test_each_enabled_model_has_required_metadata_and_builds_payload() -> None:
    for model in list_generation_models():
        assert model.provider
        assert model.generation_type
        assert model.endpoint
        assert model.docs_url
        assert _build_minimal_payload_for_model(model)


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

    assert "back_to_menu" in sections_callbacks
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


@pytest.mark.asyncio
async def test_ui_shows_placeholder_when_provider_has_no_models(monkeypatch) -> None:
    state = FakeState({"selected_generation_type": "all"})
    message = FakeMessage(chat_id=500)
    callback = FakeCallback(user_id=500, message=message, data="gen:provider:google")

    monkeypatch.setattr(generations, "list_models_by_provider", lambda provider: [])

    await generations.choose_provider(callback, state)

    assert message.edits[-1] == "У этого провайдера пока нет подключённых моделей"
    callbacks = _iter_callback_data(message.edit_markups[-1])
    assert "gen:back:sections" in callbacks