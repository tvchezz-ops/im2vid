"""Сервис для управления генерациями."""
from __future__ import annotations

from dataclasses import dataclass, field
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
    type: str
    max_images: int
    required_fields: tuple[str, ...]
    user_settings: dict[str, GenerationSetting] = field(default_factory=dict)
    system_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def model_type(self) -> str:
        """Совместимость со старым именем поля."""
        return self.type


GENERATION_MODELS: dict[str, GenerationModel] = {
    "nano_banana": GenerationModel(
        key="nano_banana",
        title="Nano Banana Pro Edit Ultra",
        endpoint="https://api.wavespeed.ai/api/v3/google/nano-banana-pro/edit-ultra",
        type="image_edit",
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
    "seedream": GenerationModel(
        key="seedream",
        title="Seedream V4.5 Edit",
        endpoint="https://api.wavespeed.ai/api/v3/bytedance/seedream-v4.5/edit",
        type="image_edit",
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
}


def get_generation_model(model_key: str) -> GenerationModel:
    """Получить конфигурацию модели по ключу."""
    try:
        return GENERATION_MODELS[model_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported generation model: {model_key}") from exc


def list_generation_models() -> list[GenerationModel]:
    """Получить список доступных моделей."""
    return list(GENERATION_MODELS.values())


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
    if not isinstance(prompt, str):
        raise ValueError("Prompt must be a string")

    invalid_image_types = [type(image_url).__name__ for image_url in image_urls if not isinstance(image_url, str)]
    if invalid_image_types:
        raise ValueError("All image URLs must be string values")

    cleaned_prompt = prompt.strip()
    valid_images = [image_url.strip() for image_url in image_urls if image_url.strip()]
    validated_settings = validate_model_settings(model_key, user_settings)

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
