"""Сервис для управления генерациями."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import GenerationRepository
from app.utils import logger


if TYPE_CHECKING:
    from app.services.wavespeed import WavespeedService


@dataclass(frozen=True)
class GenerationModel:
    """Описание модели генерации."""

    key: str
    title: str
    endpoint: str
    model_type: str
    max_images: int
    default_options: dict[str, Any] = field(default_factory=dict)


GENERATION_MODELS: dict[str, GenerationModel] = {
    "nano_banana": GenerationModel(
        key="nano_banana",
        title="Nano Banana Pro Edit Ultra",
        endpoint="https://api.wavespeed.ai/api/v3/google/nano-banana-pro/edit-ultra",
        model_type="image_edit",
        max_images=14,
        default_options={
            "aspect_ratio": "1:1",
            "resolution": "4k",
            "output_format": "png",
        },
    ),
    "seedream": GenerationModel(
        key="seedream",
        title="Seedream V4.5 Edit",
        endpoint="https://api.wavespeed.ai/api/v3/bytedance/seedream-v4.5/edit",
        model_type="image_edit",
        max_images=10,
        default_options={
            "size": "1024*1024",
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


def build_payload(model_key: str, image_urls: list[str], prompt: str) -> dict[str, Any]:
    """Собрать валидный payload для выбранной модели."""
    model = get_generation_model(model_key)
    cleaned_prompt = prompt.strip()
    valid_images = [image_url for image_url in image_urls if image_url]

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
        "enable_sync_mode": False,
        "enable_base64_output": False,
    }
    payload.update(model.default_options)
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

            # Сохраняем в БД
            generation = await self.repository.create_generation_request(
                user_id=user_id,
                model_key=model.key,
                model_endpoint=model.endpoint,
                prompt=prompt,
                cost=cost,
                input_image_urls=[],
                aspect_ratio=(options or {}).get("aspect_ratio"),
                resolution=(options or {}).get("resolution"),
                size=(options or {}).get("size"),
                output_format=(options or {}).get("output_format"),
            )
            
            # Отправляем запрос в API
            prediction_id, api_response = await self.wavespeed_service.submit_generation(
                model_key=model.key,
                images=image_urls,
                prompt=prompt,
                options=options,
            )
            await self.repository.update_generation_status(
                generation.id,
                "processing",
                wavespeed_prediction_id=prediction_id,
            )
            
            logger.info(f"Generation {generation.id} created for user {user_id}")
            return {
                "generation_id": generation.id,
                "prediction_id": prediction_id,
                "api_response": api_response,
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
                    "api_status": None,
                }

            # Получаем статус из API
            status_data = await self.wavespeed_service.get_result(generation.wavespeed_prediction_id)
            
            return {
                "generation_id": generation.id,
                "prompt": generation.prompt,
                "status": generation.status.value,
                "api_status": status_data,
            }
        except Exception as e:
            logger.exception("Error getting generation status: %s", e)
            raise
