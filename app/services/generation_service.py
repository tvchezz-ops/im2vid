"""Сервис для управления генерациями."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import GenerationRepository
from app.utils import logger


if TYPE_CHECKING:
    from app.services.wavespeed import WavespeedService


@dataclass(frozen=True)
class SettingOption:
    """Допустимое значение пользовательской настройки модели."""

    value: str
    label: str


@dataclass(frozen=True)
class GenerationSetting:
    """Описание одной пользовательской настройки модели."""

    key: str
    title: str
    type: str
    default: str
    options: tuple[SettingOption, ...]

    @property
    def allowed_values(self) -> set[str]:
        """Получить множество допустимых значений."""
        return {option.value for option in self.options}


@dataclass(frozen=True)
class GenerationModel:
    """Описание модели генерации."""

    key: str
    title: str
    endpoint: str
    provider: str
    # lipsync = audio/text driven face animation (talking avatar)
    generation_type: str
    max_images: int
    required_fields: tuple[str, ...]
    user_settings: dict[str, GenerationSetting] = field(default_factory=dict)
    system_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        """Совместимость со старым именем поля."""
        return self.generation_type

    @property
    def model_type(self) -> str:
        """Совместимость со старым именем поля."""
        return self.generation_type


GENERATION_TYPES = [
    "text_to_image",
    "text_to_video",
    "image_to_image",
    "image_to_video",
    "video_to_video",
    "lipsync",
]

PROVIDERS = [
    "alibaba",
    "openai",
    "bytedance",
    "google",
    "midjourney",
]


def infer_generation_type_from_endpoint(endpoint: str) -> str:
    """Определить тип генерации по endpoint Wavespeed docs/API."""
    normalized_endpoint = endpoint.strip().lower()
    endpoint_type_map = (
        ("lipsync", "lipsync"),
        ("talking", "lipsync"),
        ("avatar", "lipsync"),
        ("speech-to-video", "lipsync"),
        ("voice-to-video", "lipsync"),
        ("audio-to-video", "lipsync"),
        ("text-to-image", "text_to_image"),
        ("text-to-video", "text_to_video"),
        ("image-to-image", "image_to_image"),
        ("image-to-video", "image_to_video"),
        ("video-to-video", "video_to_video"),
    )

    for endpoint_marker, generation_type in endpoint_type_map:
        if endpoint_marker in normalized_endpoint:
            return generation_type

    return ""


def build_model_registry(models: tuple[GenerationModel, ...]) -> dict[str, GenerationModel]:
    """Собрать и провалидировать реестр моделей для будущей синхронизации с docs."""
    registry: dict[str, GenerationModel] = {}

    for model in models:
        generation_type = model.generation_type or infer_generation_type_from_endpoint(model.endpoint)
        if not generation_type:
            logger.warning(
                "Skipping generation model with unsupported endpoint pattern: %s (%s)",
                model.key,
                model.endpoint,
            )
            continue
        if generation_type != model.generation_type:
            model = replace(model, generation_type=generation_type)
        if model.provider not in PROVIDERS:
            raise ValueError(f"Unsupported provider '{model.provider}' for model '{model.key}'")
        if model.generation_type not in GENERATION_TYPES:
            raise ValueError(
                f"Unsupported generation type '{model.generation_type}' for model '{model.key}'"
            )
        if model.key in registry:
            raise ValueError(f"Duplicate generation model key: {model.key}")
        registry[model.key] = model

    return registry


# TODO: auto-sync models from Wavespeed docs.
MODEL_REGISTRY = build_model_registry((
    GenerationModel(
        key="nano_banana",
        title="Nano Banana Pro Edit Ultra",
        endpoint="https://api.wavespeed.ai/api/v3/google/nano-banana-pro/edit-ultra",
        provider="google",
        generation_type="image_to_image",
        max_images=14,
        required_fields=("images", "prompt"),
        user_settings={
            "aspect_ratio": GenerationSetting(
                key="aspect_ratio",
                title="Формат",
                type="select",
                default="1:1",
                options=tuple(SettingOption(value=value, label=value) for value in (
                    "1:1",
                    "3:2",
                    "2:3",
                    "3:4",
                    "4:3",
                    "4:5",
                    "5:4",
                    "9:16",
                    "16:9",
                    "21:9",
                )),
            ),
            "resolution": GenerationSetting(
                key="resolution",
                title="Разрешение",
                type="select",
                default="4k",
                options=(
                    SettingOption(value="4k", label="4k"),
                    SettingOption(value="8k", label="8k"),
                ),
            ),
            "output_format": GenerationSetting(
                key="output_format",
                title="Формат файла",
                type="select",
                default="png",
                options=(
                    SettingOption(value="png", label="png"),
                    SettingOption(value="jpeg", label="jpeg"),
                ),
            ),
        },
        system_settings={
            "enable_sync_mode": False,
            "enable_base64_output": False,
        },
    ),
    GenerationModel(
        key="seedream",
        title="Seedream V4.5 Edit",
        endpoint="https://api.wavespeed.ai/api/v3/bytedance/seedream-v4.5/edit",
        provider="bytedance",
        generation_type="image_to_image",
        max_images=10,
        required_fields=("images", "prompt"),
        user_settings={
            "size": GenerationSetting(
                key="size",
                title="Размер",
                type="select",
                default="1024*1024",
                options=tuple(SettingOption(value=value, label=value) for value in (
                    "512*512",
                    "768*768",
                    "1024*1024",
                    "1280*720",
                    "720*1280",
                    "1536*1536",
                    "2048*2048",
                    "4096*4096",
                )),
            ),
        },
        system_settings={
            "enable_sync_mode": False,
            "enable_base64_output": False,
        },
    ),
))

# Backward-compatible alias for existing imports.
GENERATION_MODELS = MODEL_REGISTRY


def get_generation_model(model_key: str) -> GenerationModel:
    """Получить конфигурацию модели по ключу."""
    try:
        return MODEL_REGISTRY[model_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported generation model: {model_key}") from exc


def list_generation_models() -> list[GenerationModel]:
    """Получить список доступных моделей."""
    return list(MODEL_REGISTRY.values())


def _filter_models(
    *,
    generation_type: Optional[str] = None,
    provider: Optional[str] = None,
) -> list[GenerationModel]:
    """Отфильтровать валидные модели из реестра по типу и/или провайдеру."""
    models = list_generation_models()

    if generation_type is not None:
        models = [model for model in models if model.generation_type == generation_type]
    if provider is not None:
        models = [model for model in models if model.provider == provider]

    return models


def list_generation_types() -> list[str]:
    """Получить типы генерации, для которых есть валидные модели в реестре."""
    return [
        generation_type
        for generation_type in GENERATION_TYPES
        if _filter_models(generation_type=generation_type)
    ]


def list_providers() -> list[str]:
    """Получить провайдеров, для которых есть валидные модели в реестре."""
    return [provider for provider in PROVIDERS if _filter_models(provider=provider)]


def list_models_by_type(generation_type: str) -> list[GenerationModel]:
    """Получить валидные модели по типу генерации."""
    return _filter_models(generation_type=generation_type)


def list_models_by_provider(provider: str) -> list[GenerationModel]:
    """Получить валидные модели по провайдеру."""
    return _filter_models(provider=provider)


def list_models_by_type_and_provider(
    generation_type: str,
    provider: str,
) -> list[GenerationModel]:
    """Получить валидные модели по типу генерации и провайдеру."""
    return _filter_models(generation_type=generation_type, provider=provider)


def get_default_settings(model_key: str) -> dict[str, Any]:
    """Получить настройки модели по умолчанию."""
    model = get_generation_model(model_key)
    return {
        setting_key: setting.default
        for setting_key, setting in model.user_settings.items()
    }


def validate_model_settings(
    model_key: str,
    settings: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Провалидировать пользовательские настройки модели и вернуть допустимые значения."""
    model = get_generation_model(model_key)
    raw_settings = dict(settings or {})
    validated_settings = get_default_settings(model_key)

    for setting_key, setting in model.user_settings.items():
        raw_value = raw_settings.get(setting_key)
        if raw_value is None or raw_value == "":
            continue
        if not isinstance(raw_value, str):
            raise ValueError(
                f"Setting '{setting_key}' for model '{model.key}' must be a string value"
            )
        if raw_value not in setting.allowed_values:
            allowed_values = ", ".join(option.value for option in setting.options)
            raise ValueError(
                f"Invalid value '{raw_value}' for setting '{setting_key}' in model '{model.key}'. "
                f"Allowed values: {allowed_values}"
            )
        validated_settings[setting_key] = raw_value

    return validated_settings


def build_payload(
    model_key: str,
    image_urls: list[str],
    prompt: str,
    user_settings: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Собрать валидный payload для выбранной модели."""
    model = get_generation_model(model_key)
    raw_user_settings = dict(user_settings or {})
    if not isinstance(prompt, str):
        raise ValueError("Prompt must be a string")

    invalid_image_types = [type(image_url).__name__ for image_url in image_urls if not isinstance(image_url, str)]
    if invalid_image_types:
        raise ValueError("All image URLs must be string values")

    cleaned_prompt = prompt.strip()
    valid_images = [image_url.strip() for image_url in image_urls if image_url.strip()]
    validated_settings = validate_model_settings(model_key, user_settings)

    if model.generation_type == "lipsync":
        media_url = valid_images[0] if valid_images else ""
        audio_value = raw_user_settings.get("audio") or raw_user_settings.get("audio_url")
        if audio_value is not None and not isinstance(audio_value, str):
            raise ValueError("Audio for lipsync models must be a string value")
        cleaned_audio = audio_value.strip() if isinstance(audio_value, str) else ""

        if not media_url:
            raise ValueError("Lipsync models require an image or video input")
        if not cleaned_prompt and not cleaned_audio:
            raise ValueError("Lipsync models require audio or text input")

        media_field = "video" if media_url.lower().split("?", 1)[0].endswith((
            ".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".mpg", ".mpeg",
        )) else "image"

        payload: dict[str, Any] = {
            media_field: media_url,
            **validated_settings,
            **model.system_settings,
        }
        if cleaned_audio:
            payload["audio"] = cleaned_audio
        if cleaned_prompt:
            payload["text"] = cleaned_prompt
        return payload

    if any(not isinstance(value, str) for value in validated_settings.values()):
        raise ValueError("All validated settings must be string values")

    if not valid_images:
        raise ValueError("At least one image URL is required")
    if len(valid_images) > model.max_images:
        raise ValueError(
            f"Model {model.key} supports at most {model.max_images} images, got {len(valid_images)}"
        )
    if not cleaned_prompt:
        raise ValueError("Prompt must not be empty")

    payload: dict[str, Any] = {
        "images": valid_images,
        "prompt": cleaned_prompt,
        **validated_settings,
        **model.system_settings,
    }
    return payload


class GenerationService:
    """Сервис для управления генерациями."""

    def __init__(self, session: AsyncSession, wavespeed_service: WavespeedService):
        """Инициализация."""
        self.repository = GenerationRepository(session)
        self.wavespeed_service = wavespeed_service

    async def create_generation(
        self,
        user_id: int,
        model_key: str,
        image_urls: list[str],
        prompt: str,
        cost: int = 1,
        options: Optional[dict[str, Any]] = None,
    ) -> Optional[dict]:
        """Создать генерацию."""
        try:
            model = get_generation_model(model_key)
            validated_options = validate_model_settings(model_key, options)
            payload = build_payload(model.key, image_urls, prompt, validated_options)

            submit_result = await self.wavespeed_service.submit_generation(
                model_key=model.key,
                payload=payload,
            )

            # Сохраняем в БД
            generation = await self.repository.create_generation_request(
                user_id=user_id,
                model_key=model.key,
                model_endpoint=model.endpoint,
                prompt=prompt,
                settings=validated_options,
                cost=cost,
                input_image_urls=[],
                aspect_ratio=validated_options.get("aspect_ratio"),
                resolution=validated_options.get("resolution"),
                size=validated_options.get("size"),
                output_format=validated_options.get("output_format"),
                status="processing",
                wavespeed_prediction_id=submit_result.prediction_id,
            )
            
            logger.info(
                {
                    "action": "generation_created",
                    "prediction_id": submit_result.prediction_id,
                    "status": "processing",
                    "model_key": model.key,
                    "outputs_count": 0,
                }
            )
            return {
                "generation_id": generation.id,
                "prediction_id": submit_result.prediction_id,
                "status": submit_result.status,
            }
        except Exception as e:
            logger.exception("Error creating generation: %s", e)
            raise

    async def get_generation_status(self, generation_id: Any) -> Optional[dict]:
        """Получить статус генерации."""
        try:
            generation = await self.repository.get_by_id(generation_id)
            if not generation:
                return None

            if not generation.wavespeed_prediction_id:
                return {
                    "generation_id": generation.id,
                    "prompt": generation.prompt,
                    "status": generation.status.value,
                    "prediction_id": None,
                    "outputs_count": 0,
                }

            # Получаем статус из API
            status_data = await self.wavespeed_service.get_result(generation.wavespeed_prediction_id)
            
            return {
                "generation_id": generation.id,
                "prompt": generation.prompt,
                "status": generation.status.value,
                "prediction_id": status_data.prediction_id,
                "outputs_count": len(status_data.outputs),
            }
        except Exception as e:
            logger.exception("Error getting generation status: %s", e)
            raise
