"""Сервис для управления генерациями."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_CEILING
from typing import Any, Literal, Mapping, Optional, TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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
        if generation_price_depends_on_duration(self):
            return "per_second_video"
        return "per_generation"

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


def _to_decimal(value: Any, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default


def is_generation_cost_estimated(model: GenerationModel) -> bool:
    return model.base_wavespeed_price_usd == Decimal("0.05") and model.pricing_rules is None


def generation_price_depends_on_duration(model: GenerationModel) -> bool:
    return bool((model.pricing_rules or {}).get("duration_multiplier_per_second"))


def _get_duration_seconds(model: GenerationModel, user_settings: Mapping[str, Any]) -> Decimal:
    raw_duration = user_settings.get("duration")
    if raw_duration is None:
        duration_setting = model.user_settings.get("duration")
        raw_duration = duration_setting.default if duration_setting is not None else "1"
    duration = _to_decimal(raw_duration, Decimal("1"))
    return max(duration, Decimal("1"))


def _get_per_image_count(user_settings: Mapping[str, Any]) -> int:
    for key in ("max_images", "num_images", "image_count"):
        raw_value = user_settings.get(key)
        if raw_value is None:
            continue
        try:
            return max(1, int(str(raw_value).strip()))
        except (TypeError, ValueError):
            return 1
    return 1


def _credit_usd_price() -> Decimal:
    return settings.credit_usd_price


def _get_int(value: Any, default: int = 1) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _get_multiplier(rules: Mapping[str, Any], rule_key: str, selected_value: Any) -> Decimal:
    if selected_value is None:
        return Decimal("1")
    multipliers = rules.get(rule_key)
    if not isinstance(multipliers, Mapping):
        return Decimal("1")
    return _to_decimal(multipliers.get(str(selected_value)), Decimal("1"))


def _get_resolution_value(settings_map: Mapping[str, Any]) -> Any:
    return settings_map.get("resolution") or settings_map.get("size")


def _get_quality_value(settings_map: Mapping[str, Any]) -> Any:
    return settings_map.get("quality") or settings_map.get("mode") or settings_map.get("shot_type")


def _get_duration_value(model: GenerationModel, settings_map: Mapping[str, Any]) -> Decimal | None:
    if "duration" not in settings_map and "duration" not in model.user_settings:
        return None
    return _get_duration_seconds(model, settings_map)


def _get_output_count(settings_map: Mapping[str, Any], rules: Mapping[str, Any]) -> int:
    fields = rules.get("output_count_fields") or ("output_count", "num_outputs", "num_images", "max_images")
    if isinstance(fields, str):
        fields = (fields,)
    for field_name in fields:
        raw_value = settings_map.get(str(field_name))
        if raw_value is not None:
            return max(1, _get_int(raw_value))
    return 1


def _format_decimal(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000001")).normalize()
    return format(normalized, "f")


def _ceil_credits(price_usd: Decimal) -> int:
    credits = (price_usd / _credit_usd_price()).to_integral_value(rounding=ROUND_CEILING)
    return max(1, int(credits))


def _calculate_generation_price_details(
    model: GenerationModel,
    user_settings: Optional[Mapping[str, Any]],
    num_generations: int = 1,
) -> tuple[Decimal, int, dict[str, Any]]:
    rules = model.pricing_rules or {}
    settings_map = dict(user_settings or {})
    requested_generations = max(1, _get_int(num_generations))
    price = model.base_wavespeed_price_usd

    resolution = _get_resolution_value(settings_map)
    quality = _get_quality_value(settings_map)
    duration = _get_duration_value(model, settings_map)
    aspect_ratio = settings_map.get("aspect_ratio")
    steps = settings_map.get("steps") or settings_map.get("num_inference_steps")

    price *= _get_multiplier(rules, "resolution_multipliers", resolution)
    price *= _get_multiplier(rules, "quality_multipliers", quality)
    price *= _get_multiplier(rules, "mode_multipliers", quality)
    price *= _get_multiplier(rules, "aspect_ratio_multipliers", aspect_ratio)

    if rules.get("duration_multiplier_per_second") and duration is not None:
        price *= duration

    steps_rule = rules.get("steps_multiplier")
    if isinstance(steps_rule, Mapping) and steps is not None:
        selected_steps = _get_int(steps, _get_int(steps_rule.get("base_steps"), 20))
        base_steps = _get_int(steps_rule.get("base_steps"), 20)
        extra_steps = max(0, selected_steps - base_steps)
        step_multiplier = _to_decimal(steps_rule.get("price_per_extra_step_multiplier"), Decimal("0"))
        price *= Decimal("1") + Decimal(extra_steps) * step_multiplier

    output_count = _get_output_count(settings_map, rules)
    if rules.get("output_count_multiplier"):
        price *= Decimal(output_count)
    else:
        price *= _get_multiplier(rules, "output_count_multipliers", output_count)

    boolean_multipliers = rules.get("boolean_multipliers")
    if isinstance(boolean_multipliers, Mapping):
        for setting_key, multiplier in boolean_multipliers.items():
            if _truthy(settings_map.get(str(setting_key))):
                price *= _to_decimal(multiplier, Decimal("1"))

    price *= settings.pricing_markup_multiplier
    price *= Decimal(requested_generations)
    credits = _ceil_credits(price)
    context = {
        "action": "generation_dynamic_price_calculated",
        "model_key": model.key,
        "base_price": _format_decimal(model.base_wavespeed_price_usd),
        "resolution": str(resolution) if resolution is not None else None,
        "quality": str(quality) if quality is not None else None,
        "duration": _format_decimal(duration) if duration is not None else None,
        "markup_multiplier": _format_decimal(settings.pricing_markup_multiplier),
        "num_generations": requested_generations,
        "final_price_usd": _format_decimal(price),
        "final_credits": credits,
    }
    return price, credits, context


def calculate_generation_price_usd(
    model: GenerationModel,
    user_settings: Optional[Mapping[str, Any]],
    num_generations: int = 1,
) -> Decimal:
    price, _, context = _calculate_generation_price_details(model, user_settings, num_generations)
    logger.info(context)
    return price


def calculate_generation_cost_credits(
    model: GenerationModel,
    user_settings: Optional[Mapping[str, Any]],
    num_generations: int = 1,
) -> int:
    _, credits, context = _calculate_generation_price_details(model, user_settings, num_generations)
    logger.info(context)
    return credits


def calculate_generation_price_quote(
    model: GenerationModel,
    user_settings: Optional[Mapping[str, Any]],
    num_generations: int = 1,
) -> tuple[Decimal, int]:
    price, credits, context = _calculate_generation_price_details(model, user_settings, num_generations)
    logger.info(context)
    return price, credits


def allocate_generation_cost_credits(
    model: GenerationModel,
    user_settings: Optional[Mapping[str, Any]],
    num_generations: int,
) -> list[int]:
    requested_generations = max(1, _get_int(num_generations))
    total_cost = calculate_generation_cost_credits(model, user_settings, requested_generations)
    base_cost = total_cost // requested_generations
    remainder = total_cost % requested_generations
    return [base_cost + (1 if index < remainder else 0) for index in range(requested_generations)]


def _estimate_settings_for_cost(model: GenerationModel, *, maximize: bool) -> dict[str, Any]:
    rules = model.pricing_rules or {}
    estimated_settings = get_default_settings(model.key)

    def choose_by_multiplier(setting_key: str, rule_key: str) -> None:
        setting = model.user_settings.get(setting_key)
        multipliers = rules.get(rule_key)
        if setting is None or not isinstance(multipliers, Mapping) or not setting.options:
            return
        candidates = [
            (option.value, _to_decimal(multipliers.get(option.value), Decimal("1")))
            for option in setting.options
        ]
        selected = max(candidates, key=lambda item: item[1]) if maximize else min(candidates, key=lambda item: item[1])
        estimated_settings[setting_key] = selected[0]

    choose_by_multiplier("resolution", "resolution_multipliers")
    choose_by_multiplier("size", "resolution_multipliers")
    choose_by_multiplier("quality", "quality_multipliers")
    choose_by_multiplier("mode", "mode_multipliers")
    choose_by_multiplier("shot_type", "quality_multipliers")
    choose_by_multiplier("aspect_ratio", "aspect_ratio_multipliers")

    duration_setting = model.user_settings.get("duration")
    if rules.get("duration_multiplier_per_second") and duration_setting is not None and duration_setting.options:
        durations = [(option.value, _to_decimal(option.value, Decimal("1"))) for option in duration_setting.options]
        selected_duration = max(durations, key=lambda item: item[1]) if maximize else min(durations, key=lambda item: item[1])
        estimated_settings["duration"] = selected_duration[0]

    for setting_key in ("num_generations", "output_count", "num_outputs", "num_images", "max_images", "steps", "num_inference_steps"):
        setting = model.user_settings.get(setting_key)
        if setting is None or not setting.options:
            continue
        numeric_options = [(option.value, _to_decimal(option.value, Decimal("1"))) for option in setting.options]
        selected_option = max(numeric_options, key=lambda item: item[1]) if maximize else min(numeric_options, key=lambda item: item[1])
        estimated_settings[setting_key] = selected_option[0]

    return estimated_settings


def estimate_minimum_generation_cost(model: GenerationModel) -> int:
    return calculate_generation_cost_credits(model, _estimate_settings_for_cost(model, maximize=False))


def estimate_maximum_generation_cost(model: GenerationModel) -> int:
    return calculate_generation_cost_credits(model, _estimate_settings_for_cost(model, maximize=True))


def get_minimum_generation_cost_credits(model: GenerationModel) -> int:
    return estimate_minimum_generation_cost(model)


def generation_cost_has_minimum_label(model: GenerationModel) -> bool:
    return bool(model.pricing_rules)


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


def _normalize_seed_setting(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Преобразовать seed к формату, который принимает Wavespeed API."""
    normalized_settings = dict(settings)
    original_seed = normalized_settings.get("seed")
    used_seed = None

    if "seed" in normalized_settings:
        try:
            seed_value = int(str(original_seed).strip())
        except (TypeError, ValueError):
            normalized_settings.pop("seed", None)
        else:
            if seed_value >= 0:
                normalized_settings["seed"] = seed_value
                used_seed = seed_value
            else:
                normalized_settings.pop("seed", None)

    logger.info(
        {
            "action": "seed_processed",
            "original_seed": original_seed,
            "used_seed": used_seed,
        }
    )
    return normalized_settings


def _remove_empty_optional_text_settings(
    model: GenerationModel,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """Убрать пустые optional text settings из payload-настроек."""
    normalized_settings = dict(settings)
    for setting_key, setting in model.user_settings.items():
        value = normalized_settings.get(setting_key)
        if setting.type != "text":
            continue
        if setting_key in model.required_payload_fields:
            continue
        if isinstance(value, str) and not value.strip():
            normalized_settings.pop(setting_key, None)
    return normalized_settings


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
        allowed_payload_fields=("prompt", "negative_prompt", "audio", "size", "duration"),
        user_settings={
            "size": _select_setting("size", "Размер", "1280*720", ("1280*720", "720*1280", "1920*1080", "1080*1920")),
            "duration": _select_setting("duration", "Длительность", "5", ("5", "8", "10", "15")),
            "negative_prompt": _text_setting("negative_prompt", "Negative prompt", "", "Что нужно исключить из результата"),
        },
        base_wavespeed_price_usd=Decimal("0.08"),
        pricing_rules=WAN_2_6_PRICING_RULES,
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
            "negative_prompt": _text_setting("negative_prompt", "Negative prompt", "", "Что нужно исключить из результата"),
        },
        base_wavespeed_price_usd=Decimal("0.11"),
        pricing_rules=WAN_2_6_PRICING_RULES,
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
            "negative_prompt": _text_setting("negative_prompt", "Negative prompt", "", "Что нужно исключить из результата"),
        },
        base_wavespeed_price_usd=Decimal("0.05"),
        pricing_rules=WAN_2_6_PRICING_RULES,
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
        },
        base_wavespeed_price_usd=Decimal("0.06"),
        pricing_rules=RUNWAY_LIKE_VIDEO_PRICING_RULES,
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
        base_wavespeed_price_usd=Decimal("0.025"),
        pricing_rules=SEEDREAM_PRICING_RULES,
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
        },
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
        base_wavespeed_price_usd=Decimal("0.03"),
        pricing_rules=SEEDREAM_PRICING_RULES,
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
        base_wavespeed_price_usd=Decimal("0.04"),
        pricing_rules=SEEDREAM_PRICING_RULES,
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
        },
        system_settings=COMMON_IMAGE_SYSTEM_SETTINGS,
        base_wavespeed_price_usd=Decimal("0.03"),
        pricing_rules=SEEDREAM_PRICING_RULES,
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
        base_wavespeed_price_usd=Decimal("0.14"),
        pricing_rules=NANO_BANANA_PRICING_RULES,
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
        base_wavespeed_price_usd=Decimal("0.22"),
        pricing_rules=VEO_3_1_PRICING_RULES,
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
        base_wavespeed_price_usd=Decimal("0.12"),
        pricing_rules=VEO_3_1_PRICING_RULES,
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
        base_wavespeed_price_usd=Decimal("0.12"),
        pricing_rules=VEO_3_1_PRICING_RULES,
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
        if setting_key == "num_generations":
            try:
                requested_generations = int(raw_value.strip())
            except ValueError as exc:
                raise ValueError(
                    f"Invalid value '{raw_value}' for setting '{setting_key}' in model '{model.key}'"
                ) from exc
            validated_settings[setting_key] = str(min(max(requested_generations, 1), 4))
            continue
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


def get_model_num_generations(model: GenerationModel, user_settings: Optional[Mapping[str, Any]] = None) -> int:
    validated_settings = validate_model_settings(model.key, user_settings)
    raw_value = validated_settings.get("num_generations", "1")
    try:
        requested_generations = int(str(raw_value).strip())
    except (TypeError, ValueError):
        requested_generations = 1
    return min(max(requested_generations, 1), 4)


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
    validated_settings = _remove_empty_optional_text_settings(
        model,
        _normalize_seed_setting(validate_model_settings(model_key, user_settings)),
    )

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

    if any(not isinstance(value, (str, int)) for value in validated_settings.values()):
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
    payload.pop("num_generations", None)
    payload.pop("num_outputs", None)
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
