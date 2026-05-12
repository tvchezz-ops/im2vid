"""Сервис для управления генерациями."""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Any, Literal, Mapping, Optional, TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import GenerationRepository
from app.services.model_registry import (
    GENERATION_CATEGORIES,
    GENERATION_MODELS,
    GENERATION_TYPES,
    MODEL_REGISTRY,
    PROVIDERS,
    GenerationModel,
    GenerationSetting,
    SettingOption,
    build_model_registry,
    create_wavespeed_model_from_docs_url,
    get_default_allowed_payload_fields,
    get_generation_model,
    is_contract_complete,
    infer_generation_type_from_endpoint,
    infer_generation_type_from_slug,
    infer_provider_from_url_or_slug,
    humanize_model_title,
    list_generation_models,
    normalize_model_key,
    normalize_generation_type,
)
from app.utils import logger


if TYPE_CHECKING:
    from app.services.wavespeed import WavespeedService


def get_required_input_type(generation_type: str) -> Literal["text", "image", "video", "lipsync"]:
    """Вернуть тип обязательного пользовательского ввода для generation_type."""
    normalized_generation_type = normalize_generation_type(generation_type)
    if normalized_generation_type in {"text_to_image", "text_to_video"}:
        return "text"
    if normalized_generation_type in {
        "image_to_image",
        "image_edit",
        "image_to_video",
        "reference_to_video",
        "motion_control",
        "effects",
    }:
        return "image"
    if normalized_generation_type in {"video_edit", "video_extend", "video_to_audio"}:
        return "video"
    if normalized_generation_type in {"lipsync", "avatar", "audio_to_video"}:
        return "lipsync"
    raise ValueError(f"Unsupported generation type: {generation_type}")


def get_model_required_input_type(model: GenerationModel) -> Literal["text", "image", "video", "lipsync"]:
    """Return the next user input type from the model's concrete media contract."""
    if model.input_media_field in {"image", "images"}:
        return "image"
    if model.input_media_field == "video":
        return "video"
    if model.requires_audio:
        return "lipsync"
    return "text"


def model_requires_media(model: GenerationModel) -> bool:
    """Проверить, что модели нужен media-вход."""
    return model.input_media_field is not None


def model_requires_image(model: GenerationModel) -> bool:
    """Проверить, что модели нужен image-вход."""
    return model.input_media_field in {"image", "images"}


def model_requires_video(model: GenerationModel) -> bool:
    """Проверить, что модели нужен video-вход."""
    return model.input_media_field == "video"


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
        setting_key: (setting.default.strip().lower() == "true" if setting.type == "boolean" else setting.default)
        for setting_key, setting in model.user_settings.items()
    }


def _format_setting_value(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _validate_number_setting(
    model: GenerationModel,
    setting: GenerationSetting,
    raw_value: str,
) -> str:
    try:
        numeric_value = Decimal(raw_value.strip())
    except Exception as exc:
        raise ValueError(
            f"Invalid numeric value '{raw_value}' for setting '{setting.key}' in model '{model.key}'"
        ) from exc

    if setting.min_value is not None and numeric_value < Decimal(str(setting.min_value)):
        raise ValueError(
            f"Value '{raw_value}' for setting '{setting.key}' in model '{model.key}' is below minimum {setting.min_value}"
        )
    if setting.max_value is not None and numeric_value > Decimal(str(setting.max_value)):
        raise ValueError(
            f"Value '{raw_value}' for setting '{setting.key}' in model '{model.key}' is above maximum {setting.max_value}"
        )
    if setting.options and raw_value not in setting.allowed_values:
        allowed_values = ", ".join(option.value for option in setting.options)
        raise ValueError(
            f"Invalid value '{raw_value}' for setting '{setting.key}' in model '{model.key}'. "
            f"Allowed values: {allowed_values}"
        )
    return str(int(numeric_value)) if numeric_value == numeric_value.to_integral_value() else str(numeric_value)


def _validate_boolean_setting(
    model: GenerationModel,
    setting: GenerationSetting,
    raw_value: Any,
) -> bool:
    normalized_value = _format_setting_value(raw_value).strip().lower()
    boolean_values = {"true", "false"}
    if setting.options:
        boolean_values.update(setting.allowed_values)
    if normalized_value not in boolean_values:
        allowed_values = ", ".join(sorted(boolean_values))
        raise ValueError(
            f"Invalid boolean value '{raw_value}' for setting '{setting.key}' in model '{model.key}'. "
            f"Allowed values: {allowed_values}"
        )
    return normalized_value == "true"


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
        if setting.type == "boolean":
            validated_settings[setting_key] = _validate_boolean_setting(model, setting, raw_value)
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
        if setting.type == "number":
            validated_settings[setting_key] = _validate_number_setting(model, setting, raw_value)
            continue
        if setting.type == "select" and raw_value not in setting.allowed_values:
            allowed_values = ", ".join(option.value for option in setting.options)
            raise ValueError(
                f"Invalid value '{raw_value}' for setting '{setting_key}' in model '{model.key}'. "
                f"Allowed values: {allowed_values}"
            )
        validated_settings[setting_key] = raw_value

    return validated_settings


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
        if field_name == "image_or_video":
            if not (payload.get("image") or payload.get("video")):
                missing_fields.append(field_name)
            continue
        if field_name == "text_or_audio":
            if not (payload.get("text") or payload.get("audio")):
                missing_fields.append(field_name)
            continue
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


def _get_input_requirement(model: GenerationModel, input_kind: str) -> Mapping[str, Any]:
    requirement = (model.input_requirements or {}).get(input_kind)
    return requirement if isinstance(requirement, Mapping) else {}


def _get_input_payload_field(model: GenerationModel, input_kind: str, default: str) -> str:
    requirement = _get_input_requirement(model, input_kind)
    payload_field = requirement.get("payload_field")
    return str(payload_field or (model.payload_mapping or {}).get(input_kind) or default)


def ensure_model_contract_ready(model: GenerationModel) -> None:
    """Fail before billing when docs-derived contract metadata is incomplete."""
    if not model.is_enabled or not is_contract_complete(model):
        raise ValueError("missing_docs_contract")


def validate_payload_contract(model: GenerationModel, payload: Mapping[str, Any]) -> None:
    unexpected_fields = sorted(set(payload) - set(model.allowed_payload_fields))
    if unexpected_fields:
        raise ValueError(f"Model '{model.key}' payload contains unsupported field(s): {', '.join(unexpected_fields)}")
    _assert_required_payload_fields(model, payload)


def build_payload(
    model_key: str,
    image_urls: list[str],
    prompt: str,
    user_settings: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Собрать валидный payload для выбранной модели."""
    model = get_generation_model(model_key)
    allowed_payload_fields = model.allowed_payload_fields or get_default_allowed_payload_fields(model)
    ensure_model_contract_ready(model)
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

    input_video_value = raw_user_settings.get("input_video_url")
    if input_video_value is not None and not isinstance(input_video_value, str):
        raise ValueError("Video input must be a string value")
    cleaned_input_video = input_video_value.strip() if isinstance(input_video_value, str) else ""
    input_audio_value = raw_user_settings.get("input_audio_url")

    if model.generation_type == "lipsync":
        media_url = cleaned_input_video or (valid_inputs[0] if valid_inputs else "")
        audio_value = input_audio_value
        if audio_value is not None and not isinstance(audio_value, str):
            raise ValueError("Audio for lipsync models must be a string value")
        cleaned_audio = audio_value.strip() if isinstance(audio_value, str) else ""

        if not media_url:
            raise ValueError("Lipsync models require a video input")
        if model.input_media_field == "video" and not media_url.lower().split("?", 1)[0].endswith((
            ".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".mpg", ".mpeg",
        )):
            raise ValueError("Lipsync models require a video input")
        if model.requires_audio and not cleaned_audio:
            raise ValueError("Lipsync audio-to-video models require audio input")
        if model.requires_prompt and not cleaned_prompt:
            raise ValueError("Prompt must not be empty")

        payload: dict[str, Any] = {
            _get_input_payload_field(model, "video", "video"): media_url,
            **validated_settings,
            **model.system_settings,
        }
        if cleaned_audio:
            payload[_get_input_payload_field(model, "audio", "audio")] = cleaned_audio
        if cleaned_prompt:
            payload[_get_input_payload_field(model, "prompt", "prompt")] = cleaned_prompt
        _apply_supported_system_flags(payload, allowed_payload_fields)
        filtered_payload = {
            key: value
            for key, value in payload.items()
            if key in allowed_payload_fields
        }
        validate_payload_contract(model, filtered_payload)
        return filtered_payload

    if any(not isinstance(value, (str, int, bool)) for value in validated_settings.values()):
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
        if not cleaned_input_video and not valid_inputs:
            raise ValueError("At least one video URL is required")
        if not cleaned_input_video and len(valid_inputs) > 1:
            raise ValueError(f"Model {model.key} supports exactly one video input")

    cleaned_audio = ""
    if model.requires_audio:
        audio_value = input_audio_value
        if audio_value is not None and not isinstance(audio_value, str):
            raise ValueError("Audio input must be a string value")
        cleaned_audio = audio_value.strip() if isinstance(audio_value, str) else ""
        if not cleaned_audio:
            raise ValueError("Audio input is required")

    payload: dict[str, Any] = {**validated_settings, **model.system_settings}
    payload.pop("num_generations", None)
    payload.pop("num_outputs", None)
    if model.requires_prompt and cleaned_prompt:
        payload[_get_input_payload_field(model, "prompt", "prompt")] = cleaned_prompt
    if model.input_media_field == "video":
        payload[_get_input_payload_field(model, "video", "video")] = cleaned_input_video or valid_inputs[0]
    elif model.input_media_field == "image":
        payload[_get_input_payload_field(model, "images", "image")] = valid_inputs[0]
    elif model.input_media_field == "images":
        payload[_get_input_payload_field(model, "images", "images")] = valid_inputs
    if model.requires_audio:
        payload[_get_input_payload_field(model, "audio", "audio")] = cleaned_audio
    _apply_supported_system_flags(payload, allowed_payload_fields)
    filtered_payload = {
        key: value
        for key, value in payload.items()
        if key in allowed_payload_fields
    }
    validate_payload_contract(model, filtered_payload)
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
