"""Сервис для управления генерациями."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal, Mapping, Optional, TYPE_CHECKING

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
    provider: str
    # lipsync = audio/text driven face animation (talking avatar)
    generation_type: str
    endpoint: str
    docs_url: str
    description: str
    max_images: int
    requires_prompt: bool
    requires_image: bool
    requires_video: bool
    requires_audio: bool
    outputs: str
    input_media_field: str | None = None
    min_images: int = 0
    supports_multiple_images: bool = False
    is_enabled: bool = True
    warning: str = ""
    required_payload_fields: tuple[str, ...] = ()
    allowed_payload_fields: tuple[str, ...] = ()
    input_schema: dict[str, Any] = field(default_factory=dict)
    user_settings: dict[str, GenerationSetting] = field(default_factory=dict)
    system_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def required_fields(self) -> tuple[str, ...]:
        """Совместимость со старой схемой обязательных полей."""
        if self.required_payload_fields:
            return self.required_payload_fields
        fields: list[str] = []
        if self.generation_type == "lipsync":
            fields.append("image_or_video")
            fields.append("text_or_audio")
            return tuple(fields)
        if self.requires_image:
            fields.append("images" if self.outputs == "image" else "image")
        if self.requires_video:
            fields.append("video")
        if self.requires_prompt:
            fields.append("prompt")
        if self.requires_audio:
            fields.append("audio")
        return tuple(fields)

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
    "image_edit",
    "image_to_video",
    "video_edit",
    "lipsync",
]

PROVIDERS = [
    "alibaba",
    "openai",
    "bytedance",
    "google",
    "midjourney",
]


def get_required_input_type(generation_type: str) -> Literal["text", "image", "video", "lipsync"]:
    """Вернуть тип обязательного пользовательского ввода для generation_type."""
    normalized_generation_type = normalize_generation_type(generation_type)
    if normalized_generation_type in {"text_to_image", "text_to_video"}:
        return "text"
    if normalized_generation_type in {"image_edit", "image_to_video"}:
        return "image"
    if normalized_generation_type == "video_edit":
        return "video"
    if normalized_generation_type == "lipsync":
        return "lipsync"
    raise ValueError(f"Unsupported generation type: {generation_type}")


def model_requires_media(model: GenerationModel) -> bool:
    """Проверить, что модели нужен media-вход."""
    return model.input_media_field is not None or get_required_input_type(model.generation_type) == "lipsync"


def model_requires_image(model: GenerationModel) -> bool:
    """Проверить, что модели нужен image-вход."""
    return model.input_media_field in {"image", "images"}


def model_requires_video(model: GenerationModel) -> bool:
    """Проверить, что модели нужен video-вход."""
    return model.input_media_field == "video"


def normalize_generation_type(generation_type: str) -> str:
    """Нормализовать legacy алиасы типов генерации к новым canonical значениям."""
    legacy_aliases = {
        "image_to_image": "image_edit",
        "video_to_video": "video_edit",
    }
    return legacy_aliases.get(generation_type, generation_type)


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
        ("image-edit", "image_edit"),
        ("/edit", "image_edit"),
        ("image-to-image", "image_edit"),
        ("image-to-video", "image_to_video"),
        ("video-edit", "video_edit"),
        ("video-to-video", "video_edit"),
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
        generation_type = normalize_generation_type(generation_type)
        if generation_type != model.generation_type:
            model = replace(model, generation_type=generation_type)
        if not model.required_payload_fields:
            model = replace(model, required_payload_fields=get_default_required_payload_fields(model))
        if not model.allowed_payload_fields:
            model = replace(model, allowed_payload_fields=get_default_allowed_payload_fields(model))
        if not model.input_schema:
            model = replace(model, input_schema=build_input_schema(model))
        if model.provider not in PROVIDERS:
            raise ValueError(f"Unsupported provider '{model.provider}' for model '{model.key}'")
        if model.generation_type not in GENERATION_TYPES:
            raise ValueError(
                f"Unsupported generation type '{model.generation_type}' for model '{model.key}'"
            )
        if model.outputs not in {"image", "video"}:
            raise ValueError(f"Unsupported outputs '{model.outputs}' for model '{model.key}'")
        if not model.is_enabled and not model.warning:
            model = replace(model, warning="Model is disabled")
        if model.key in registry:
            raise ValueError(f"Duplicate generation model key: {model.key}")
        registry[model.key] = model

    return registry


def _select_setting(
    key: str,
    title: str,
    default: str,
    values: tuple[str, ...],
) -> GenerationSetting:
    return GenerationSetting(
        key=key,
        title=title,
        type="select",
        default=default,
        options=tuple(SettingOption(value=value, label=value) for value in values),
    )


def _text_setting(
    key: str,
    title: str,
    default: str,
) -> GenerationSetting:
    return GenerationSetting(
        key=key,
        title=title,
        type="text",
        default=default,
        options=(),
    )


def _api_endpoint(provider: str, path: str) -> str:
    return f"https://api.wavespeed.ai/api/v3/{provider}/{path}"


def _docs_url(provider: str, slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/{provider}/{slug}"


def _model(
    *,
    key: str,
    title: str,
    provider: str,
    generation_type: str,
    path: str,
    slug: str,
    description: str,
    outputs: str,
    requires_prompt: bool,
    input_media_field: str | None = None,
    min_images: int = 0,
    requires_image: bool = False,
    requires_video: bool = False,
    requires_audio: bool = False,
    max_images: int = 0,
    supports_multiple_images: bool = False,
    is_enabled: bool = True,
    warning: str = "",
    required_payload_fields: tuple[str, ...] = (),
    allowed_payload_fields: tuple[str, ...] = (),
    input_schema: Optional[dict[str, Any]] = None,
    user_settings: Optional[dict[str, GenerationSetting]] = None,
    system_settings: Optional[dict[str, Any]] = None,
) -> GenerationModel:
    return GenerationModel(
        key=key,
        title=title,
        provider=provider,
        generation_type=generation_type,
        endpoint=_api_endpoint(provider, path),
        docs_url=_docs_url(provider, slug),
        description=description,
        input_media_field=input_media_field,
        min_images=min_images,
        max_images=max_images,
        supports_multiple_images=supports_multiple_images,
        requires_prompt=requires_prompt,
        requires_image=requires_image,
        requires_video=requires_video,
        requires_audio=requires_audio,
        outputs=outputs,
        is_enabled=is_enabled,
        warning=warning,
        required_payload_fields=required_payload_fields,
        allowed_payload_fields=allowed_payload_fields,
        input_schema=input_schema or {},
        user_settings=user_settings or {},
        system_settings=system_settings or {},
    )


NANO_BANANA_SETTINGS = {
    "aspect_ratio": _select_setting(
        "aspect_ratio",
        "Формат",
        "1:1",
        (
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
        ),
    ),
    "resolution": _select_setting("resolution", "Разрешение", "4k", ("4k", "8k")),
    "output_format": _select_setting(
        "output_format",
        "Формат файла",
        "png",
        ("png", "jpeg"),
    ),
}

SEEDREAM_EDIT_SETTINGS = {
    "size": _select_setting(
        "size",
        "Размер",
        "1024*1024",
        (
            "512*512",
            "768*768",
            "1024*1024",
            "1280*720",
            "720*1280",
            "1536*1536",
            "2048*2048",
            "4096*4096",
        ),
    ),
}

COMMON_IMAGE_SYSTEM_SETTINGS = {
    "enable_sync_mode": False,
    "enable_base64_output": False,
}


def get_default_allowed_payload_fields(model: GenerationModel) -> tuple[str, ...]:
    """Получить безопасный whitelist payload-полей по умолчанию для модели."""
    fields: list[str] = []
    if model.generation_type == "lipsync":
        fields.extend(["image", "video", "text", "audio"])
    else:
        if model.requires_prompt:
            fields.append("prompt")
        if model.input_media_field:
            fields.append(model.input_media_field)
    fields.extend(str(key) for key in model.system_settings.keys())
    return tuple(dict.fromkeys(fields))


def get_default_required_payload_fields(model: GenerationModel) -> tuple[str, ...]:
    """Получить обязательные payload-поля по умолчанию на основе сигнатуры модели."""
    return model.required_fields


def build_input_schema(model: GenerationModel) -> dict[str, Any]:
    """Собрать декларативное описание допустимых параметров модели."""
    return {
        "input_media_field": model.input_media_field,
        "min_images": model.min_images,
        "max_images": model.max_images,
        "supports_multiple_images": model.supports_multiple_images,
        "required_payload_fields": list(model.required_payload_fields),
        "allowed_payload_fields": list(model.allowed_payload_fields),
        "user_settings": {
            setting_key: {
                "type": setting.type,
                "default": setting.default,
                "options": [option.value for option in setting.options],
            }
            for setting_key, setting in model.user_settings.items()
        },
    }


# TODO: auto-sync models from Wavespeed docs.
MODEL_REGISTRY = build_model_registry((
    _model(
        key="alibaba_wan_2_7_text_to_image",
        title="Alibaba Wan 2.7 Text To Image",
        provider="alibaba",
        generation_type="text_to_image",
        path="wan-2.7/text-to-image",
        slug="alibaba-wan-2.7-text-to-image",
        description="Alibaba Wan 2.7 text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        is_enabled=False,
        warning="Parameters need verification",
    ),
    _model(
        key="alibaba_wan_2_6_text_to_image",
        title="Alibaba Wan 2.6 Text To Image",
        provider="alibaba",
        generation_type="text_to_image",
        path="wan-2.6/text-to-image",
        slug="alibaba-wan-2.6-text-to-image",
        description="Alibaba Wan 2.6 text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "width", "height", "enable_prompt_expansion", "seed"),
        user_settings={
            "width": _select_setting("width", "Ширина", "1024", ("512", "768", "1024", "1280", "1536")),
            "height": _select_setting("height", "Высота", "1024", ("512", "768", "1024", "1280", "1536")),
            "enable_prompt_expansion": _select_setting("enable_prompt_expansion", "Улучшение prompt", "false", ("false", "true")),
            "seed": _text_setting("seed", "Seed", "-1"),
        },
    ),
    _model(
        key="alibaba_wan_2_6_text_to_video",
        title="Alibaba Wan 2.6 Text To Video",
        provider="alibaba",
        generation_type="text_to_video",
        path="wan-2.6/text-to-video",
        slug="alibaba-wan-2.6-text-to-video",
        description="Alibaba Wan 2.6 text-to-video model from Wavespeed docs.",
        outputs="video",
        requires_prompt=True,
        input_media_field=None,
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "negative_prompt", "audio", "size"),
        user_settings={
            "size": _select_setting("size", "Размер", "1280*720", ("1280*720", "720*1280", "1920*1080", "1080*1920")),
            "negative_prompt": _text_setting("negative_prompt", "Negative prompt", ""),
        },
    ),
    _model(
        key="alibaba_wan_2_6_image_to_video",
        title="Alibaba Wan 2.6 Image To Video",
        provider="alibaba",
        generation_type="image_to_video",
        path="wan-2.6/image-to-video",
        slug="alibaba-wan-2.6-image-to-video",
        description="Alibaba Wan 2.6 image-to-video model from Wavespeed docs.",
        outputs="video",
        requires_prompt=True,
        input_media_field="image",
        min_images=1,
        requires_image=True,
        max_images=1,
        is_enabled=False,
        warning="Parameters need verification",
    ),
    _model(
        key="alibaba_wan_2_6_image_to_video_pro",
        title="Alibaba Wan 2.6 Image To Video Pro",
        provider="alibaba",
        generation_type="image_to_video",
        path="wan-2.6/image-to-video-pro",
        slug="alibaba-wan-2.6-image-to-video-pro",
        description="Alibaba Wan 2.6 Image To Video Pro model from Wavespeed docs.",
        outputs="video",
        requires_prompt=True,
        input_media_field="image",
        min_images=1,
        requires_image=True,
        max_images=1,
        required_payload_fields=("image", "prompt"),
        allowed_payload_fields=("image", "prompt", "audio", "negative_prompt", "resolution", "duration", "shot_type"),
        user_settings={
            "resolution": _select_setting("resolution", "Разрешение", "1080p", ("1080p", "2k", "4k")),
            "duration": _select_setting("duration", "Длительность", "5", ("5", "8", "10", "15")),
            "shot_type": _select_setting("shot_type", "Тип кадра", "single", ("single", "multi")),
            "negative_prompt": _text_setting("negative_prompt", "Negative prompt", ""),
        },
    ),
    _model(
        key="alibaba_wan_2_6_image_to_video_flash",
        title="Alibaba Wan 2.6 Image To Video Flash",
        provider="alibaba",
        generation_type="image_to_video",
        path="wan-2.6/image-to-video-flash",
        slug="alibaba-wan-2.6-image-to-video-flash",
        description="Alibaba Wan 2.6 Image To Video Flash model from Wavespeed docs.",
        outputs="video",
        requires_prompt=True,
        input_media_field="image",
        min_images=1,
        requires_image=True,
        max_images=1,
        required_payload_fields=("image", "prompt"),
        allowed_payload_fields=("image", "prompt", "audio", "negative_prompt", "resolution", "duration", "shot_type"),
        user_settings={
            "resolution": _select_setting("resolution", "Разрешение", "720p", ("720p", "1080p")),
            "duration": _select_setting("duration", "Длительность", "15", ("5", "8", "10", "15")),
            "shot_type": _select_setting("shot_type", "Тип кадра", "single", ("single", "multi")),
            "negative_prompt": _text_setting("negative_prompt", "Negative prompt", ""),
        },
    ),
    _model(
        key="alibaba_happyhorse_1_0_image_to_video",
        title="Alibaba Happyhorse 1.0 Image To Video",
        provider="alibaba",
        generation_type="image_to_video",
        path="happyhorse-1.0/image-to-video",
        slug="alibaba-happyhorse-1.0-image-to-video",
        description="Alibaba Happyhorse 1.0 image-to-video model from Wavespeed docs.",
        outputs="video",
        requires_prompt=True,
        input_media_field="image",
        min_images=1,
        requires_image=True,
        max_images=1,
        is_enabled=False,
        warning="Parameters need verification",
    ),
    _model(
        key="alibaba_happyhorse_1_0_text_to_video",
        title="Alibaba Happyhorse 1.0 Text To Video",
        provider="alibaba",
        generation_type="text_to_video",
        path="happyhorse-1.0/text-to-video",
        slug="alibaba-happyhorse-1.0-text-to-video",
        description="Alibaba Happyhorse 1.0 text-to-video model from Wavespeed docs.",
        outputs="video",
        requires_prompt=True,
        input_media_field=None,
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "aspect_ratio", "resolution", "duration", "seed"),
        user_settings={
            "aspect_ratio": _select_setting("aspect_ratio", "Формат", "16:9", ("16:9", "9:16", "1:1", "4:3", "3:4")),
            "resolution": _select_setting("resolution", "Разрешение", "720p", ("720p", "1080p")),
            "duration": _select_setting("duration", "Длительность", "5", ("3", "5", "8", "10", "15")),
            "seed": _text_setting("seed", "Seed", "-1"),
        },
    ),
    _model(
        key="openai_gpt_image_2_text_to_image",
        title="OpenAI GPT Image 2 Text To Image",
        provider="openai",
        generation_type="text_to_image",
        path="gpt-image-2/text-to-image",
        slug="openai-gpt-image-2-text-to-image",
        description="OpenAI GPT Image 2 text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        is_enabled=False,
        warning="Parameters need verification from docs",
    ),
    _model(
        key="openai_gpt_image_2_edit",
        title="OpenAI GPT Image 2 Edit",
        provider="openai",
        generation_type="image_edit",
        path="gpt-image-2/edit",
        slug="openai-gpt-image-2-edit",
        description="OpenAI GPT Image 2 edit model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field="images",
        min_images=1,
        requires_image=True,
        max_images=10,
        supports_multiple_images=True,
        is_enabled=False,
        warning="Parameters need verification from docs",
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
    _model(
        key="openai_gpt_image_1_text_to_image",
        title="OpenAI GPT Image 1 Text To Image",
        provider="openai",
        generation_type="text_to_image",
        path="gpt-image-1/text-to-image",
        slug="openai-gpt-image-1-text-to-image",
        description="OpenAI GPT Image 1 text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        is_enabled=False,
        warning="Parameters need verification from docs",
    ),
    _model(
        key="openai_gpt_image_1_5_text_to_image",
        title="OpenAI GPT Image 1.5 Text To Image",
        provider="openai",
        generation_type="text_to_image",
        path="gpt-image-1.5/text-to-image",
        slug="openai-gpt-image-1.5-text-to-image",
        description="OpenAI GPT Image 1.5 text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        is_enabled=False,
        warning="Parameters need verification from docs",
    ),
    _model(
        key="openai_gpt_image_1_mini_text_to_image",
        title="OpenAI GPT Image 1 Mini Text To Image",
        provider="openai",
        generation_type="text_to_image",
        path="gpt-image-1-mini/text-to-image",
        slug="openai-gpt-image-1-mini-text-to-image",
        description="OpenAI GPT Image 1 Mini text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        is_enabled=False,
        warning="Parameters need verification from docs",
    ),
    _model(
        key="bytedance_seedream_v5_0_lite",
        title="Bytedance Seedream V5.0 Lite",
        provider="bytedance",
        generation_type="text_to_image",
        path="seedream-v5.0-lite",
        slug="bytedance-seedream-v5.0-lite",
        description="ByteDance Seedream V5.0 Lite text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        is_enabled=False,
        warning="Parameters need verification",
    ),
    _model(
        key="bytedance_seedream_v5_0_lite_sequential",
        title="Bytedance Seedream V5.0 Lite Sequential",
        provider="bytedance",
        generation_type="text_to_image",
        path="seedream-v5.0-lite-sequential",
        slug="bytedance-seedream-v5.0-lite-sequential",
        description="ByteDance Seedream V5.0 Lite Sequential model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "output_format", "enable_base64_output", "enable_sync_mode"),
        user_settings={
            "output_format": _select_setting("output_format", "Формат файла", "jpeg", ("jpeg", "png")),
        },
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
    _model(
        key="bytedance_seedream_v4_sequential",
        title="Bytedance Seedream V4 Sequential",
        provider="bytedance",
        generation_type="text_to_image",
        path="seedream-v4-sequential",
        slug="bytedance-seedream-v4-sequential",
        description="ByteDance Seedream V4 Sequential text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "size", "max_images", "enable_sync_mode", "enable_base64_output"),
        user_settings={
            "size": _select_setting("size", "Размер", "1024*1024", ("512*512", "768*768", "1024*1024", "1280*720", "720*1280", "2048*2048")),
            "max_images": _select_setting("max_images", "Количество изображений", "1", ("1", "2", "3", "4")),
        },
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
    _model(
        key="bytedance_seedream_v4_5",
        title="Bytedance Seedream V4.5",
        provider="bytedance",
        generation_type="text_to_image",
        path="seedream-v4.5",
        slug="bytedance-seedream-v4.5",
        description="ByteDance Seedream V4.5 text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        is_enabled=False,
        warning="Parameters need verification",
    ),
    _model(
        key="bytedance_seedream_v4_5_edit",
        title="Bytedance Seedream V4.5 Edit",
        provider="bytedance",
        generation_type="image_edit",
        path="seedream-v4.5/edit",
        slug="bytedance-seedream-v4.5-edit",
        description="ByteDance Seedream V4.5 edit model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field="images",
        min_images=1,
        requires_image=True,
        max_images=10,
        supports_multiple_images=True,
        required_payload_fields=("images", "prompt"),
        allowed_payload_fields=("images", "prompt", "size", "enable_sync_mode", "enable_base64_output"),
        user_settings=SEEDREAM_EDIT_SETTINGS,
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
    _model(
        key="bytedance_seedream_v3_1",
        title="Bytedance Seedream V3.1",
        provider="bytedance",
        generation_type="text_to_image",
        path="seedream-v3.1",
        slug="bytedance-seedream-v3.1",
        description="ByteDance Seedream V3.1 text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "size", "seed", "enable_prompt_expansion", "enable_sync_mode", "enable_base64_output"),
        user_settings={
            "size": _select_setting("size", "Размер", "1024*1024", ("512*512", "768*768", "1024*1024", "1280*720", "720*1280", "2048*2048")),
            "seed": _text_setting("seed", "Seed", "-1"),
            "enable_prompt_expansion": _select_setting("enable_prompt_expansion", "Улучшение prompt", "false", ("false", "true")),
        },
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
    _model(
        key="bytedance_seedream_v3",
        title="Bytedance Seedream V3",
        provider="bytedance",
        generation_type="text_to_image",
        path="seedream-v3",
        slug="bytedance-seedream-v3",
        description="ByteDance Seedream V3 text-to-image model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field=None,
        is_enabled=False,
        warning="Parameters need verification",
    ),
    _model(
        key="bytedance_lipsync",
        title="Bytedance Lipsync Audio To Video",
        provider="bytedance",
        generation_type="lipsync",
        path="lipsync-audio-to-video",
        slug="bytedance-lipsync-audio-to-video",
        description="ByteDance lipsync avatar model from Wavespeed docs.",
        outputs="video",
        requires_prompt=False,
        input_media_field=None,
        max_images=1,
        is_enabled=False,
        warning="Parameters need verification",
    ),
    _model(
        key="avatar_omni_human",
        title="Avatar Omni Human",
        provider="bytedance",
        generation_type="lipsync",
        path="avatar-omni-human",
        slug="bytedance-avatar-omni-human",
        description="ByteDance talking avatar model from Wavespeed docs.",
        outputs="video",
        requires_prompt=False,
        input_media_field=None,
        max_images=1,
        is_enabled=False,
        warning="Parameters need verification",
    ),
    _model(
        key="google_nano_banana_pro_edit_ultra",
        title="Google Nano Banana Pro Edit Ultra",
        provider="google",
        generation_type="image_edit",
        path="nano-banana-pro/edit-ultra",
        slug="google-nano-banana-pro-edit-ultra",
        description="Google Nano Banana Pro Edit Ultra model from Wavespeed docs.",
        outputs="image",
        requires_prompt=True,
        input_media_field="images",
        min_images=1,
        requires_image=True,
        max_images=14,
        supports_multiple_images=True,
        required_payload_fields=("images", "prompt"),
        allowed_payload_fields=("images", "prompt", "aspect_ratio", "resolution", "output_format", "enable_sync_mode", "enable_base64_output"),
        user_settings=NANO_BANANA_SETTINGS,
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
    _model(
        key="google_veo3",
        title="Google Veo3",
        provider="google",
        generation_type="text_to_video",
        path="veo3",
        slug="google-veo3",
        description="Google Veo3 text-to-video model from Wavespeed docs.",
        outputs="video",
        requires_prompt=True,
        input_media_field=None,
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "duration", "resolution", "aspect_ratio", "enable_sync_mode", "enable_base64_output"),
        user_settings={
            "duration": _select_setting("duration", "Длительность", "8", ("5", "8")),
            "resolution": _select_setting("resolution", "Разрешение", "720p", ("720p", "1080p")),
            "aspect_ratio": _select_setting("aspect_ratio", "Формат", "16:9", ("16:9", "9:16")),
        },
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
    _model(
        key="google_veo3_fast",
        title="Google Veo3 Fast",
        provider="google",
        generation_type="text_to_video",
        path="veo3-fast",
        slug="google-veo3-fast",
        description="Google Veo3 Fast text-to-video model from Wavespeed docs.",
        outputs="video",
        requires_prompt=True,
        input_media_field=None,
        required_payload_fields=("prompt",),
        allowed_payload_fields=("prompt", "duration", "resolution", "aspect_ratio", "enable_sync_mode", "enable_base64_output"),
        user_settings={
            "duration": _select_setting("duration", "Длительность", "8", ("5", "8")),
            "resolution": _select_setting("resolution", "Разрешение", "720p", ("720p", "1080p")),
            "aspect_ratio": _select_setting("aspect_ratio", "Формат", "16:9", ("16:9", "9:16")),
        },
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
    _model(
        key="google_veo3_1_fast_video_extend",
        title="Google Veo3.1 Fast Video Extend",
        provider="google",
        generation_type="video_edit",
        path="veo3.1-fast/video-extend",
        slug="google-veo3.1-fast-video-extend",
        description="Google Veo3.1 Fast video extend model from Wavespeed docs.",
        outputs="video",
        requires_prompt=False,
        input_media_field="video",
        requires_video=True,
        min_images=1,
        max_images=1,
        required_payload_fields=("video",),
        allowed_payload_fields=("video", "prompt", "enable_sync_mode", "enable_base64_output"),
        user_settings={
            "prompt": _text_setting("prompt", "Описание продолжения", ""),
        },
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
    ),
))

LEGACY_MODEL_KEY_ALIASES = {
    "nano_banana": "google_nano_banana_pro_edit_ultra",
    "seedream": "bytedance_seedream_v4_5_edit",
    "gpt_image_2_text_to_image": "openai_gpt_image_2_text_to_image",
    "gpt_image_2_edit": "openai_gpt_image_2_edit",
    "seedream_v4_5": "bytedance_seedream_v4_5",
}

# Backward-compatible alias for existing imports.
GENERATION_MODELS = MODEL_REGISTRY


def get_generation_model(model_key: str) -> GenerationModel:
    """Получить конфигурацию модели по ключу."""
    try:
        canonical_key = LEGACY_MODEL_KEY_ALIASES.get(model_key, model_key)
        return MODEL_REGISTRY[canonical_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported generation model: {model_key}") from exc


def list_generation_models() -> list[GenerationModel]:
    """Получить список доступных моделей."""
    return [model for model in MODEL_REGISTRY.values() if model.is_enabled]


def _filter_models(
    *,
    generation_type: Optional[str] = None,
    provider: Optional[str] = None,
) -> list[GenerationModel]:
    """Отфильтровать валидные модели из реестра по типу и/или провайдеру."""
    models = list_generation_models()

    if generation_type is not None:
        generation_type = normalize_generation_type(generation_type)
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
    """Получить провайдеров для каталога пользовательского выбора."""
    return list(PROVIDERS)


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
        if setting.type == "select" and raw_value not in setting.allowed_values:
            allowed_values = ", ".join(option.value for option in setting.options)
            raise ValueError(
                f"Invalid value '{raw_value}' for setting '{setting_key}' in model '{model.key}'. "
                f"Allowed values: {allowed_values}"
            )
        validated_settings[setting_key] = raw_value

    return validated_settings


def _apply_supported_system_flags(
    payload: dict[str, Any],
    allowed_payload_fields: tuple[str, ...],
) -> None:
    for field_name in ("enable_base64_output", "enable_sync_mode"):
        if field_name in allowed_payload_fields and field_name not in payload:
            payload[field_name] = False


def _assert_required_payload_fields(
    model: GenerationModel,
    payload: Mapping[str, Any],
) -> None:
    missing_fields: list[str] = []
    for field_name in model.required_payload_fields:
        value = payload.get(field_name)
        if value is None:
            missing_fields.append(field_name)
            continue
        if isinstance(value, str) and not value.strip():
            missing_fields.append(field_name)
            continue
        if isinstance(value, list) and not value:
            missing_fields.append(field_name)
    if missing_fields:
        missing = ", ".join(missing_fields)
        raise ValueError(f"Model '{model.key}' requires payload field(s): {missing}")


def build_payload(
    model_key: str,
    image_urls: list[str],
    prompt: str,
    user_settings: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Собрать валидный payload для выбранной модели."""
    model = get_generation_model(model_key)
    allowed_payload_fields = model.allowed_payload_fields or get_default_allowed_payload_fields(model)
    if not model.is_enabled:
        warning_suffix = f" ({model.warning})" if model.warning else ""
        raise ValueError(f"Model '{model.key}' is disabled{warning_suffix}")
    raw_user_settings = dict(user_settings or {})
    if not isinstance(prompt, str):
        raise ValueError("Prompt must be a string")

    invalid_image_types = [type(image_url).__name__ for image_url in image_urls if not isinstance(image_url, str)]
    if invalid_image_types:
        raise ValueError("All input URLs must be string values")

    cleaned_prompt = prompt.strip()
    valid_inputs = [image_url.strip() for image_url in image_urls if image_url.strip()]
    validated_settings = validate_model_settings(model_key, user_settings)

    if model.generation_type == "lipsync":
        media_url = valid_inputs[0] if valid_inputs else ""
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
        _apply_supported_system_flags(payload, allowed_payload_fields)
        filtered_payload = {
            key: value
            for key, value in payload.items()
            if key in allowed_payload_fields
        }
        _assert_required_payload_fields(model, filtered_payload)
        return filtered_payload

    if any(not isinstance(value, str) for value in validated_settings.values()):
        raise ValueError("All validated settings must be string values")

    if model.requires_prompt and not cleaned_prompt:
        raise ValueError("Prompt must not be empty")

    if model.input_media_field == "images":
        if not valid_inputs:
            raise ValueError("At least one image URL is required")
        if len(valid_inputs) < model.min_images:
            raise ValueError(f"Model {model.key} requires at least {model.min_images} images")
        if model.max_images and len(valid_inputs) > model.max_images:
            raise ValueError(
                f"Model {model.key} supports at most {model.max_images} images, got {len(valid_inputs)}"
            )
    elif model.input_media_field == "image":
        if not valid_inputs:
            raise ValueError("At least one image URL is required")
        if len(valid_inputs) > 1:
            raise ValueError(f"Model {model.key} supports exactly one image input")
    elif model.input_media_field == "video":
        if not valid_inputs:
            raise ValueError("At least one video URL is required")
        if len(valid_inputs) > 1:
            raise ValueError(f"Model {model.key} supports exactly one video input")

    payload: dict[str, Any] = {**validated_settings, **model.system_settings}
    if model.requires_prompt and cleaned_prompt:
        payload["prompt"] = cleaned_prompt
    if model.input_media_field == "video":
        payload["video"] = valid_inputs[0]
    elif model.input_media_field == "image":
        payload["image"] = valid_inputs[0]
    elif model.input_media_field == "images":
        payload["images"] = valid_inputs
    _apply_supported_system_flags(payload, allowed_payload_fields)
    filtered_payload = {
        key: value
        for key, value in payload.items()
        if key in allowed_payload_fields
    }
    _assert_required_payload_fields(model, filtered_payload)
    return filtered_payload


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
