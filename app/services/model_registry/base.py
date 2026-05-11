"""Base generation model registry types and construction helpers."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from app.utils import logger


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
    description: str = ""

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
    base_wavespeed_price_usd: Decimal = Decimal("0.05")
    pricing_rules: dict[str, Any] | None = None

    @property
    def wavespeed_price_usd(self) -> Decimal:
        """Backward-compatible name for the provider base price."""
        return self.base_wavespeed_price_usd

    @property
    def fallback_price_usd(self) -> Decimal:
        """Backward-compatible default used for unknown model pricing."""
        return Decimal("0.05")

    @property
    def pricing_type(self) -> str:
        """Backward-compatible coarse pricing type."""
        if (self.pricing_rules or {}).get("duration_multiplier_per_second"):
            return "per_second_video"
        return "per_generation"

    @property
    def required_fields(self) -> tuple[str, ...]:
        """Совместимость со старой схемой обязательных полей."""
        if self.required_payload_fields:
            return self.required_payload_fields
        fields: list[str] = []
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


GENERATION_CATEGORIES = [
    "text_to_image",
    "image_to_image",
    "image_edit",
    "text_to_video",
    "image_to_video",
    "reference_to_video",
    "video_edit",
    "video_extend",
    "lipsync",
    "motion_control",
    "avatar",
    "audio_to_video",
    "video_to_audio",
    "effects",
    "all_models",
]

GENERATION_TYPES = [
    generation_category
    for generation_category in GENERATION_CATEGORIES
    if generation_category != "all_models"
]

PROVIDERS = [
    "alibaba",
    "bytedance",
    "google",
    "openai",
    "kling",
    "grok",
    "minimax",
    "wavespeed_ai",
]


def normalize_generation_type(generation_type: str) -> str:
    """Нормализовать legacy алиасы типов генерации к новым canonical значениям."""
    legacy_aliases = {
        "video_to_video": "video_edit",
    }
    return legacy_aliases.get(generation_type, generation_type)


def _slug_tokens(value: str) -> list[str]:
    normalized_value = value.strip().lower()
    parsed_url = urlparse(normalized_value)
    if parsed_url.scheme or parsed_url.netloc:
        normalized_value = parsed_url.path
    return [token for token in re.split(r"[^a-z0-9]+", normalized_value) if token]


def _slug_text(value: str) -> str:
    return "-".join(_slug_tokens(value))


def _has_token_sequence(tokens: list[str], *sequence: str) -> bool:
    sequence_length = len(sequence)
    return any(tokens[index:index + sequence_length] == list(sequence) for index in range(len(tokens) - sequence_length + 1))


def infer_generation_type_from_slug(slug: str) -> str:
    """Infer generation type from a model slug or docs/API path."""
    tokens = _slug_tokens(slug)
    slug_text = "-".join(tokens)
    if not tokens:
        return ""

    marker_map = (
        (("lipsync",), "lipsync"),
        (("avatar",), "avatar"),
        (("effects",), "effects"),
        (("motion-control",), "motion_control"),
        (("text-to-image", "t2i"), "text_to_image"),
        (("image-to-image",), "image_to_image"),
        (("text-to-video", "t2v"), "text_to_video"),
        (("image-to-video", "i2v"), "image_to_video"),
        (("reference-to-video",), "reference_to_video"),
        (("video-edit", "v2v"), "video_edit"),
        (("video-extend",), "video_extend"),
        (("audio-to-video", "speech-to-video"), "audio_to_video"),
        (("video-to-audio",), "video_to_audio"),
        (("image-edit",), "image_edit"),
    )
    for markers, generation_type in marker_map:
        if any(marker in slug_text for marker in markers):
            return generation_type

    if "edit" in tokens and "image" in tokens:
        return "image_edit"
    if "edit" in tokens:
        return "image_edit"
    if "seededit" in tokens:
        return "image_edit"
    if "seedream" in tokens:
        return "text_to_image"
    if "sora" in tokens:
        return "text_to_video"
    if any(token.startswith("veo") for token in tokens):
        return "text_to_video"
    if any(token.startswith("imagen") for token in tokens):
        return "text_to_image"
    if "gpt" in tokens and "image" in tokens:
        return "text_to_image"
    if "dall" in tokens:
        return "text_to_image"
    if "nano" in tokens and "banana" in tokens:
        return "text_to_image"
    if "grok" in tokens and "image" in tokens:
        return "text_to_image"
    if "kling" in tokens and ("elements" in tokens or _has_token_sequence(tokens, "multi", "shot")):
        return "reference_to_video"
    if "kling" in tokens and "image" in tokens:
        return "text_to_image"
    if "hailuo" in tokens:
        if "i2v" in tokens:
            return "image_to_video"
        return "text_to_video"
    if "video" in tokens and any(token in tokens for token in {"01", "02", "fast", "pro", "standard"}):
        return "text_to_video"
    if "animate" in tokens or _has_token_sequence(tokens, "fun", "control") or "flf2v" in tokens:
        return "image_to_video"
    return ""


def infer_provider_from_url_or_slug(value: str) -> str:
    """Infer supported provider key from a docs URL, API URL, or slug."""
    slug_text = _slug_text(value)
    provider_markers = (
        ("wavespeed_ai", ("wavespeed-ai", "wavespeedai", "wavespeed")),
        ("bytedance", ("bytedance", "byte-dance")),
        ("openai", ("openai", "open-ai")),
        ("minimax", ("minimax", "mini-max")),
        ("alibaba", ("alibaba",)),
        ("google", ("google",)),
        ("kling", ("kling",)),
        ("grok", ("x-ai", "xai", "grok")),
    )
    for provider, markers in provider_markers:
        if any(marker in slug_text for marker in markers):
            return provider
    return ""


def normalize_model_key(slug: str) -> str:
    """Normalize a provider/model slug into a stable registry key."""
    return "_".join(_slug_tokens(slug))


def humanize_model_title(slug: str) -> str:
    """Build a readable model title from a provider/model slug."""
    title_overrides = {
        "ai": "AI",
        "api": "API",
        "gpt": "GPT",
        "i2v": "I2V",
        "minimax": "MiniMax",
        "openai": "OpenAI",
        "t2i": "T2I",
        "t2v": "T2V",
    }
    title_parts = []
    tokens = _slug_tokens(slug)
    token_index = 0
    while token_index < len(tokens):
        token = tokens[token_index]
        if token.isdigit() and token_index + 1 < len(tokens) and tokens[token_index + 1].isdigit():
            title_parts.append(f"{token}.{tokens[token_index + 1]}")
            token_index += 2
            continue
        compact_version = re.fullmatch(r"([a-z]+)(\d+)", token)
        if compact_version and token_index + 1 < len(tokens) and tokens[token_index + 1].isdigit():
            title_parts.append(f"{compact_version.group(1).capitalize()}{compact_version.group(2)}.{tokens[token_index + 1]}")
            token_index += 2
            continue
        if token in title_overrides:
            title_parts.append(title_overrides[token])
        else:
            title_parts.append(token.capitalize())
        token_index += 1
    return " ".join(title_parts)


def _extract_slug_from_url_or_slug(value: str) -> str:
    parsed_url = urlparse(value.strip())
    if parsed_url.scheme or parsed_url.netloc:
        path_parts = [part for part in parsed_url.path.split("/") if part]
        return path_parts[-1] if path_parts else ""
    return value.strip().strip("/")


def _infer_model_io(generation_type: str, slug: str = "") -> dict[str, Any]:
    slug_text = _slug_text(slug)
    outputs = "image" if generation_type in {"text_to_image", "image_to_image", "image_edit"} else "video"
    contract: dict[str, Any] = {
        "outputs": outputs,
        "requires_prompt": False,
        "requires_image": False,
        "requires_video": False,
        "requires_audio": False,
        "input_media_field": None,
        "min_images": 0,
        "max_images": 0,
        "supports_multiple_images": False,
        "is_enabled": True,
        "warning": "",
    }
    if generation_type in {"text_to_image", "text_to_video"}:
        contract["requires_prompt"] = True
    elif generation_type in {"image_edit", "image_to_image"}:
        contract.update(
            requires_prompt=True,
            requires_image=True,
            input_media_field="images",
            min_images=1,
            max_images=10 if generation_type == "image_edit" else 1,
            supports_multiple_images=generation_type == "image_edit",
        )
    elif generation_type in {"image_to_video", "reference_to_video"}:
        contract.update(
            requires_prompt=True,
            requires_image=True,
            input_media_field="image",
            min_images=1,
            max_images=1,
        )
    elif generation_type in {"video_edit", "video_extend"}:
        contract.update(requires_prompt=True, requires_video=True, input_media_field="video")
    elif generation_type == "video_to_audio":
        contract.update(requires_video=True, input_media_field="video")
    elif generation_type == "audio_to_video":
        contract.update(requires_audio=True)
    elif generation_type == "lipsync":
        if "audio-to-video" in slug_text:
            contract.update(requires_video=True, requires_audio=True, input_media_field="video")
        elif "text-to-video" in slug_text:
            contract.update(requires_prompt=True, requires_video=True, input_media_field="video")
        else:
            contract.update(is_enabled=False, warning="Lipsync input contract needs verification")
    elif generation_type in {"motion_control", "effects", "avatar"}:
        if "image-to-video" in slug_text:
            contract.update(
                requires_prompt=True,
                requires_image=True,
                input_media_field="image",
                min_images=1,
                max_images=1,
            )
        elif "text-to-video" in slug_text:
            contract.update(requires_prompt=True)
        elif "video-edit" in slug_text:
            contract.update(requires_prompt=True, requires_video=True, input_media_field="video")
        else:
            contract.update(is_enabled=False, warning=f"{generation_type} input contract needs verification")
    return contract


def create_wavespeed_model_from_docs_url(
    url: str,
    provider: str | None = None,
    price_usd: Decimal | None = None,
    enabled: bool = True,
) -> GenerationModel:
    """Create a conservative GenerationModel from a Wavespeed docs URL."""
    slug = _extract_slug_from_url_or_slug(url)
    if not slug:
        raise ValueError("Wavespeed docs URL does not contain a model slug")

    resolved_provider = provider or infer_provider_from_url_or_slug(url) or infer_provider_from_url_or_slug(slug)
    if resolved_provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider inferred from Wavespeed docs URL: {resolved_provider or 'unknown'}")

    generation_type = infer_generation_type_from_slug(slug) or infer_generation_type_from_slug(url)
    if generation_type not in GENERATION_TYPES:
        raise ValueError(f"Unsupported generation type inferred from Wavespeed docs URL: {generation_type or 'unknown'}")

    user_settings = {"num_generations": _build_num_generations_setting()}
    io_metadata = _infer_model_io(generation_type, slug)
    inferred_enabled = enabled and bool(io_metadata.pop("is_enabled"))
    inferred_warning = str(io_metadata.pop("warning") or "")
    pricing_rules = {"duration_multiplier_per_second": True} if io_metadata["outputs"] == "video" and "duration" in user_settings else None
    model = GenerationModel(
        key=normalize_model_key(slug),
        title=humanize_model_title(slug),
        provider=resolved_provider,
        generation_type=generation_type,
        endpoint=f"https://api.wavespeed.ai/api/v3/{resolved_provider}/{slug}",
        docs_url=url,
        description=f"{humanize_model_title(slug)} model from Wavespeed docs.",
        is_enabled=inferred_enabled,
        warning="" if inferred_enabled else inferred_warning or "Model is disabled",
        required_payload_fields=(),
        allowed_payload_fields=(),
        input_schema={},
        user_settings=user_settings,
        system_settings={},
        base_wavespeed_price_usd=price_usd or Decimal("0.05"),
        pricing_rules=pricing_rules,
        **io_metadata,
    )
    model = replace(
        model,
        required_payload_fields=get_default_required_payload_fields(model),
        allowed_payload_fields=get_default_allowed_payload_fields(model),
    )
    return replace(model, input_schema=build_input_schema(model))


def infer_generation_type_from_endpoint(endpoint: str) -> str:
    """Определить тип генерации по endpoint Wavespeed docs/API."""
    inferred_from_slug = infer_generation_type_from_slug(endpoint)
    if inferred_from_slug:
        return inferred_from_slug

    normalized_endpoint = endpoint.strip().lower()
    endpoint_type_map = (
        ("lipsync", "lipsync"),
        ("talking-avatar", "avatar"),
        ("avatar", "avatar"),
        ("speech-to-video", "audio_to_video"),
        ("voice-to-video", "audio_to_video"),
        ("audio-to-video", "audio_to_video"),
        ("reference-to-video", "reference_to_video"),
        ("motion-control", "motion_control"),
        ("video-to-audio", "video_to_audio"),
        ("video-extend", "video_extend"),
        ("extend", "video_extend"),
        ("effects", "effects"),
        ("text-to-image", "text_to_image"),
        ("text-to-video", "text_to_video"),
        ("image-edit", "image_edit"),
        ("/edit", "image_edit"),
        ("image-to-image", "image_to_image"),
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
    description: str = "",
) -> GenerationSetting:
    return GenerationSetting(
        key=key,
        title=title,
        description=description,
        type="select",
        default=default,
        options=tuple(SettingOption(value=value, label=value) for value in values),
    )


def _build_num_generations_setting(max_generations: int = 4) -> GenerationSetting:
    capped_limit = max(1, min(4, max_generations))
    values = tuple(str(value) for value in range(1, capped_limit + 1))
    return _select_setting(
        "num_generations",
        "Количество генераций",
        "1",
        values,
        "Сколько генераций запустить за один запрос",
    )


def _text_setting(
    key: str,
    title: str,
    default: str,
    description: str = "",
) -> GenerationSetting:
    return GenerationSetting(
        key=key,
        title=title,
        description=description,
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
    base_wavespeed_price_usd: Decimal = Decimal("0.05"),
    pricing_rules: Optional[dict[str, Any]] = None,
    wavespeed_price_usd: Optional[Decimal] = None,
    pricing_type: Optional[str] = None,
) -> GenerationModel:
    normalized_user_settings = dict(user_settings or {})
    if is_enabled:
        normalized_user_settings.setdefault(
            "num_generations",
            _build_num_generations_setting(),
        )
    resolved_base_price = wavespeed_price_usd if wavespeed_price_usd is not None else base_wavespeed_price_usd
    resolved_pricing_rules = pricing_rules
    if resolved_pricing_rules is None and pricing_type == "per_second_video":
        resolved_pricing_rules = {"duration_multiplier_per_second": True}

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
        user_settings=normalized_user_settings,
        system_settings=system_settings or {},
        base_wavespeed_price_usd=resolved_base_price,
        pricing_rules=resolved_pricing_rules,
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

ASPECT_RATIO_PRICING_MULTIPLIERS = {
    "1:1": 1.0,
    "3:2": 1.05,
    "2:3": 1.05,
    "3:4": 1.1,
    "4:3": 1.1,
    "4:5": 1.1,
    "5:4": 1.1,
    "16:9": 1.2,
    "9:16": 1.2,
    "21:9": 1.35,
}

IMAGE_SIZE_PRICING_MULTIPLIERS = {
    "512*512": 0.7,
    "768*768": 0.85,
    "1024*1024": 1.0,
    "1280*720": 1.1,
    "720*1280": 1.1,
    "1536*1536": 1.8,
    "2048*2048": 2.5,
    "4096*4096": 4.0,
}

VIDEO_RESOLUTION_PRICING_MULTIPLIERS = {
    "720p": 1.0,
    "1080p": 1.8,
    "2k": 2.4,
    "4k": 3.5,
    "1280*720": 1.0,
    "720*1280": 1.0,
    "1920*1080": 1.8,
    "1080*1920": 1.8,
}

NANO_BANANA_PRICING_RULES = {
    "resolution_multipliers": {
        "4k": 1.0,
        "8k": 2.0,
    },
    "aspect_ratio_multipliers": ASPECT_RATIO_PRICING_MULTIPLIERS,
}

VEO_3_1_PRICING_RULES = {
    "resolution_multipliers": VIDEO_RESOLUTION_PRICING_MULTIPLIERS,
    "aspect_ratio_multipliers": {
        "16:9": 1.0,
        "9:16": 1.0,
    },
    "duration_multiplier_per_second": True,
    "quality_multipliers": {
        "fast": 1.0,
        "standard": 1.5,
        "high": 2.2,
    },
}

WAN_2_6_PRICING_RULES = {
    "resolution_multipliers": VIDEO_RESOLUTION_PRICING_MULTIPLIERS,
    "quality_multipliers": {
        "single": 1.0,
        "multi": 1.3,
        "fast": 1.0,
        "standard": 1.4,
        "high": 2.0,
    },
    "aspect_ratio_multipliers": ASPECT_RATIO_PRICING_MULTIPLIERS,
    "duration_multiplier_per_second": True,
}

SEEDREAM_PRICING_RULES = {
    "resolution_multipliers": IMAGE_SIZE_PRICING_MULTIPLIERS,
    "output_count_fields": ("max_images", "num_images", "output_count"),
    "output_count_multiplier": True,
}

FLUX_PRICING_RULES = {
    "resolution_multipliers": IMAGE_SIZE_PRICING_MULTIPLIERS,
    "quality_multipliers": {
        "fast": 1.0,
        "standard": 1.4,
        "high": 2.1,
    },
    "steps_multiplier": {
        "base_steps": 20,
        "price_per_extra_step_multiplier": 0.03,
    },
}

KLING_PRICING_RULES = {
    "resolution_multipliers": VIDEO_RESOLUTION_PRICING_MULTIPLIERS,
    "quality_multipliers": {
        "standard": 1.0,
        "pro": 1.8,
        "master": 2.4,
    },
    "aspect_ratio_multipliers": ASPECT_RATIO_PRICING_MULTIPLIERS,
    "duration_multiplier_per_second": True,
}

HUNYUAN_PRICING_RULES = {
    "resolution_multipliers": VIDEO_RESOLUTION_PRICING_MULTIPLIERS,
    "quality_multipliers": {
        "fast": 1.0,
        "standard": 1.3,
        "high": 1.9,
    },
    "aspect_ratio_multipliers": ASPECT_RATIO_PRICING_MULTIPLIERS,
    "duration_multiplier_per_second": True,
    "steps_multiplier": {
        "base_steps": 30,
        "price_per_extra_step_multiplier": 0.02,
    },
}

RUNWAY_LIKE_VIDEO_PRICING_RULES = {
    "resolution_multipliers": VIDEO_RESOLUTION_PRICING_MULTIPLIERS,
    "quality_multipliers": {
        "turbo": 0.8,
        "fast": 1.0,
        "standard": 1.5,
        "high": 2.2,
    },
    "aspect_ratio_multipliers": ASPECT_RATIO_PRICING_MULTIPLIERS,
    "duration_multiplier_per_second": True,
    "boolean_multipliers": {
        "upscale": 1.7,
        "enable_upscale": 1.7,
    },
}


def get_default_allowed_payload_fields(model: GenerationModel) -> tuple[str, ...]:
    """Получить безопасный whitelist payload-полей по умолчанию для модели."""
    fields: list[str] = []
    if model.requires_prompt:
        fields.append("prompt")
    if model.input_media_field:
        fields.append(model.input_media_field)
    if model.requires_audio:
        fields.append("audio")
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
