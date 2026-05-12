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
    min_value: str | None = None
    max_value: str | None = None
    is_user_visible: bool = True

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
    payload_mapping: dict[str, str] = field(default_factory=dict)
    input_requirements: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    user_settings: dict[str, GenerationSetting] = field(default_factory=dict)
    system_settings: dict[str, Any] = field(default_factory=dict)
    base_wavespeed_price_usd: Decimal = Decimal("0.05")
    pricing_rules: dict[str, Any] | None = None
    hidden_reason: str = ""

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

MEDIA_INPUT_FIELDS = frozenset(
    {
        "image",
        "images",
        "image_url",
        "image_urls",
        "input_image",
        "input_images",
        "video",
        "video_url",
        "input_video",
        "videos",
        "video_urls",
        "audio",
        "audio_url",
        "input_audio",
        "first_frame",
        "last_frame",
        "first_image",
        "last_image",
        "start_image",
        "end_image",
        "reference_image",
        "reference_images",
        "reference_url",
        "reference_urls",
        "face_image",
        "source_image",
        "target_image",
        "element_refer_list",
    }
)


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


def _model_slug_text(model: GenerationModel) -> str:
    return _slug_text(" ".join((model.key, model.endpoint, model.docs_url)))


def _is_audio_to_video_prompt_required(slug_text: str) -> bool:
    return any(marker in slug_text for marker in ("prompt", "text", "t2v"))


def apply_docs_input_contract(model: GenerationModel) -> GenerationModel:
    """Derive runtime flow flags from generated docs input_requirements."""
    requirements = build_input_requirements(model)
    required_payload_fields = tuple(str(field_name) for field_name in model.required_payload_fields)

    def is_required(input_kind: str) -> bool:
        requirement = requirements.get(input_kind)
        if not isinstance(requirement, Mapping):
            return False
        payload_field = str(requirement.get("payload_field") or "")
        return bool(requirement.get("required")) or payload_field in required_payload_fields

    image_requirement = requirements.get("images") if isinstance(requirements.get("images"), Mapping) else {}
    video_requirement = requirements.get("video") if isinstance(requirements.get("video"), Mapping) else {}
    audio_requirement = requirements.get("audio") if isinstance(requirements.get("audio"), Mapping) else {}

    requires_prompt = is_required("prompt")
    requires_image = is_required("images")
    requires_video = is_required("video")
    requires_audio = is_required("audio")
    generation_type = normalize_generation_type(model.generation_type)

    if generation_type in {"image_to_video", "image_edit", "image_to_image", "reference_to_video"} and image_requirement:
        requires_image = True
        image_requirement = {**dict(image_requirement), "required": True}
        requirements["images"] = image_requirement
    if generation_type in {"video_edit", "video_extend", "video_to_audio"} and video_requirement:
        requires_video = True
        video_requirement = {**dict(video_requirement), "required": True}
        requirements["video"] = video_requirement
    if generation_type in {"text_to_image", "text_to_video"}:
        requires_prompt = True

    input_media_field: str | None = None
    min_images = int(image_requirement.get("min") or (1 if requires_image else model.min_images or 0)) if image_requirement else model.min_images
    max_images = int(image_requirement.get("max") or model.max_images or (1 if requires_image else 0)) if image_requirement else model.max_images
    supports_multiple_images = bool(model.supports_multiple_images)
    if image_requirement and requires_image:
        image_payload_field = str(image_requirement.get("payload_field") or "image")
        supports_multiple_images = image_payload_field in {"images", "image_urls", "input_images", "reference_images", "reference_urls", "element_refer_list"}
        if not supports_multiple_images:
            max_images = 1
        input_media_field = "images" if supports_multiple_images else "image"
    if video_requirement and requires_video:
        input_media_field = "video"

    if requires_image and generation_type in {"text_to_image", "text_to_video"}:
        generation_type = "image_edit" if model.outputs == "image" else "image_to_video"
    if requires_video and generation_type in {"text_to_image", "text_to_video"}:
        generation_type = "video_edit" if model.outputs == "video" else generation_type
    if requires_video and requires_audio and generation_type != "lipsync":
        generation_type = "lipsync"

    allowed_fields = list(model.allowed_payload_fields)
    for field_name in model.system_settings:
        if field_name not in allowed_fields:
            allowed_fields.append(field_name)
    for requirement in (image_requirement, video_requirement, audio_requirement, requirements.get("prompt")):
        if isinstance(requirement, Mapping) and requirement.get("payload_field"):
            payload_field = str(requirement["payload_field"])
            if payload_field not in allowed_fields:
                allowed_fields.append(payload_field)

    is_enabled = model.is_enabled
    warning = model.warning
    hidden_reason = model.hidden_reason
    if requires_image and requires_video and not requires_audio:
        is_enabled = False
        warning = warning or "Model requires multiple media input types not supported by the bot flow"
        hidden_reason = hidden_reason or "missing_docs_contract"
    if generation_type in {"image_to_video", "image_edit", "image_to_image", "reference_to_video"} and not requires_image:
        is_enabled = False
        warning = warning or "Model image input contract is missing in docs"
        hidden_reason = hidden_reason or "missing_docs_contract"
    if generation_type in {"video_edit", "video_extend", "video_to_audio"} and not requires_video:
        is_enabled = False
        warning = warning or "Model video input contract is missing in docs"
        hidden_reason = hidden_reason or "missing_docs_contract"

    return replace(
        model,
        generation_type=generation_type,
        requires_prompt=requires_prompt,
        requires_image=requires_image,
        requires_video=requires_video,
        requires_audio=requires_audio,
        input_media_field=input_media_field,
        min_images=min_images,
        max_images=max_images,
        supports_multiple_images=supports_multiple_images,
        is_enabled=is_enabled,
        warning=warning,
        hidden_reason=hidden_reason,
        allowed_payload_fields=tuple(dict.fromkeys(allowed_fields)),
        payload_mapping=build_payload_mapping(model),
        input_requirements=requirements,
    )


def apply_deterministic_input_contract(model: GenerationModel) -> GenerationModel:
    """Enforce input requirements from generation_type/slug so docs coverage gaps stay usable."""
    slug_text = _model_slug_text(model)
    generation_type = normalize_generation_type(model.generation_type)
    outputs = "image" if generation_type in {"text_to_image", "image_to_image", "image_edit"} else "video"
    requires_prompt = False
    requires_image = False
    requires_video = False
    requires_audio = False
    input_media_field: str | None = None
    min_images = 0
    max_images = 0
    supports_multiple_images = False
    is_enabled = model.is_enabled
    warning = model.warning

    if generation_type in {"text_to_image", "text_to_video"}:
        requires_prompt = True
    elif generation_type in {"image_to_video", "image_to_image", "image_edit"}:
        requires_prompt = True
        requires_image = True
        input_media_field = "images" if generation_type in {"image_to_image", "image_edit"} else "image"
        min_images = 1
        max_images = max(model.max_images, 10 if generation_type == "image_edit" else 1)
        supports_multiple_images = generation_type == "image_edit"
    elif generation_type == "reference_to_video":
        requires_prompt = True
        requires_image = True
        input_media_field = "images"
        min_images = 1
        max_images = max(model.max_images, 2)
        supports_multiple_images = True
    elif generation_type in {"video_edit", "video_extend"}:
        requires_prompt = True
        requires_video = True
        input_media_field = "video"
    elif generation_type == "video_to_audio":
        requires_video = True
        input_media_field = "video"
    elif generation_type == "audio_to_video":
        requires_audio = True
        requires_prompt = _is_audio_to_video_prompt_required(slug_text)
        if any(marker in slug_text for marker in ("video", "avatar", "face")) and "audio-to-video" not in slug_text and "speech-to-video" not in slug_text:
            requires_video = True
            input_media_field = "video"
    elif generation_type == "lipsync":
        requires_video = True
        input_media_field = "video"
        if "audio-to-video" in slug_text:
            requires_audio = True
        elif "text-to-video" in slug_text:
            requires_prompt = True
        else:
            is_enabled = False
            warning = warning or "Lipsync input contract needs verification"
    elif generation_type == "motion_control":
        if "image-to-video" in slug_text or "i2v" in slug_text:
            requires_prompt = True
            requires_image = True
            input_media_field = "image"
            min_images = 1
            max_images = 1
        elif "text-to-video" in slug_text or "t2v" in slug_text:
            requires_prompt = True
        else:
            is_enabled = False
            warning = warning or "Motion control input contract needs verification"
    elif generation_type in {"effects", "avatar"}:
        if any(marker in slug_text for marker in ("image-to-video", "i2v", "image", "avatar")):
            requires_prompt = "text" in slug_text or "prompt" in slug_text
            requires_image = True
            input_media_field = "image"
            min_images = 1
            max_images = 1
        elif "text-to-video" in slug_text or "t2v" in slug_text:
            requires_prompt = True
        elif "video" in slug_text:
            requires_video = True
            input_media_field = "video"
        else:
            is_enabled = False
            warning = warning or f"{generation_type} input contract needs verification"

    required_fields: list[str] = []
    if requires_image:
        required_fields.append(input_media_field or "image")
    if requires_video:
        required_fields.append("video")
    if requires_audio:
        required_fields.append("audio")
    if requires_prompt:
        required_fields.append("prompt")

    allowed_fields = list(model.allowed_payload_fields)
    for field_name in required_fields:
        if field_name not in allowed_fields:
            allowed_fields.append(field_name)
    payload_mapping = dict(model.payload_mapping or {})
    if requires_prompt:
        payload_mapping.setdefault("prompt", "prompt")
    if requires_image and input_media_field:
        payload_mapping.setdefault("images", input_media_field)
    if requires_video:
        payload_mapping.setdefault("video", "video")
    if requires_audio:
        payload_mapping.setdefault("audio", "audio")

    normalized_model = replace(
        model,
        generation_type=generation_type,
        outputs=outputs,
        requires_prompt=requires_prompt,
        requires_image=requires_image,
        requires_video=requires_video,
        requires_audio=requires_audio,
        input_media_field=input_media_field,
        min_images=min_images,
        max_images=max_images,
        supports_multiple_images=supports_multiple_images,
        is_enabled=is_enabled,
        warning=warning,
        required_payload_fields=tuple(dict.fromkeys(required_fields)),
        allowed_payload_fields=tuple(dict.fromkeys(allowed_fields)),
        payload_mapping=payload_mapping,
    )
    return replace(normalized_model, input_requirements=build_input_requirements(normalized_model))


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
        default_allowed_fields = set(get_default_allowed_payload_fields(model))
        allowed_was_missing = not model.allowed_payload_fields or set(model.allowed_payload_fields) <= default_allowed_fields
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
        if model.input_requirements and model.required_payload_fields and model.allowed_payload_fields:
            model = apply_docs_input_contract(model)
        else:
            model = apply_deterministic_input_contract(model)
        if not model.required_payload_fields:
            model = replace(model, required_payload_fields=get_default_required_payload_fields(model))
        if not model.allowed_payload_fields:
            model = replace(model, allowed_payload_fields=get_default_allowed_payload_fields(model))
        if not model.payload_mapping:
            model = replace(model, payload_mapping=build_payload_mapping(model))
        if not model.input_schema:
            model = replace(model, input_schema=build_input_schema(model))
        media_user_settings = sorted(set(model.user_settings) & MEDIA_INPUT_FIELDS)
        if media_user_settings:
            logger.warning(
                {
                    "action": "media_input_fields_removed_from_user_settings",
                    "model_key": model.key,
                    "media_fields": media_user_settings,
                }
            )
            model = replace(
                model,
                user_settings={
                    setting_key: setting
                    for setting_key, setting in model.user_settings.items()
                    if setting_key not in MEDIA_INPUT_FIELDS
                },
            )
            model = replace(model, input_schema=build_input_schema(model))
        model = apply_fallback_user_settings(model, allowed_was_missing=allowed_was_missing)
        model = replace(model, payload_mapping=build_payload_mapping(model))
        model = replace(model, input_schema=build_input_schema(model))
        if model.provider not in PROVIDERS:
            raise ValueError(f"Unsupported provider '{model.provider}' for model '{model.key}'")
        if model.generation_type not in GENERATION_TYPES:
            raise ValueError(
                f"Unsupported generation type '{model.generation_type}' for model '{model.key}'"
            )
        if model.outputs not in {"image", "video"}:
            raise ValueError(f"Unsupported outputs '{model.outputs}' for model '{model.key}'")
        if model.is_enabled and not is_contract_complete(model):
            model = replace(
                model,
                is_enabled=False,
                warning=model.warning or "Model contract is incomplete",
                hidden_reason=model.hidden_reason or "missing_docs_contract",
            )
        if not model.is_enabled and not model.warning:
            model = replace(model, warning="Model is disabled")
        if model.key in registry:
            raise ValueError(f"Duplicate generation model key: {model.key}")
        registry[model.key] = model

    enabled_models = [model for model in registry.values() if model.is_enabled]
    only_num_generations = [model.key for model in enabled_models if _has_only_num_generations_settings(model)]
    logger.info(
        {
            "action": "model_registry_params_summary",
            "enabled_models": len(enabled_models),
            "models_with_generated_params": sum(1 for model in enabled_models if not _has_only_num_generations_settings(model)),
            "models_with_only_num_generations": len(only_num_generations),
            "models_requiring_audio": sum(1 for model in enabled_models if model.requires_audio),
            "models_requiring_video": sum(1 for model in enabled_models if model.requires_video),
            "models_requiring_images": sum(1 for model in enabled_models if model.requires_image),
        }
    )
    if only_num_generations:
        logger.warning(
            {
                "action": "model_registry_params_low_coverage",
                "only_num_generations_count": len(only_num_generations),
                "sample_model_keys": only_num_generations[:20],
            }
        )
    return registry


def _normalize_generated_setting_type(setting_type: str) -> str:
    normalized_type = setting_type.strip().lower()
    if normalized_type in {"enum", "select"}:
        return "select"
    if normalized_type in {"string", "text"}:
        return "text"
    if normalized_type in {"integer", "number", "float"}:
        return "number"
    if normalized_type in {"boolean", "bool", "toggle"}:
        return "boolean"
    return normalized_type or "text"


def _coerce_setting_options(raw_options: Any) -> tuple[SettingOption, ...]:
    if raw_options is None:
        return ()
    if not isinstance(raw_options, (list, tuple)):
        raw_options = [raw_options]

    options: list[SettingOption] = []
    for raw_option in raw_options:
        if isinstance(raw_option, Mapping):
            raw_value = raw_option.get("value")
            if raw_value is None:
                continue
            label = str(raw_option.get("label", raw_value))
            options.append(SettingOption(value=str(raw_value), label=label))
            continue
        options.append(SettingOption(value=str(raw_option), label=str(raw_option)))
    return tuple(options)


def generation_setting_from_generated(key: str, data: Mapping[str, Any]) -> GenerationSetting:
    """Build a GenerationSetting from generated Wavespeed docs metadata."""
    setting_type = _normalize_generated_setting_type(str(data.get("type", "text")))
    options = _coerce_setting_options(data.get("options"))
    if setting_type == "boolean" and not options:
        options = (
            SettingOption(value="false", label="Off"),
            SettingOption(value="true", label="On"),
        )
    default = data.get("default")
    if default is None:
        if options:
            default = options[0].value
        elif setting_type == "boolean":
            default = "false"
        else:
            default = ""
    return GenerationSetting(
        key=str(data.get("key", key)),
        title=str(data.get("title", key.replace("_", " ").title())),
        type=setting_type,
        default=str(default).lower() if isinstance(default, bool) else str(default),
        options=options,
        description=str(data.get("description", "")),
        min_value=None if data.get("min_value") is None else str(data.get("min_value")),
        max_value=None if data.get("max_value") is None else str(data.get("max_value")),
        is_user_visible=bool(data.get("is_user_visible", True)),
    )


def _coerce_decimal(value: Any, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def apply_generated_model_params(
    models: tuple[GenerationModel, ...],
    generated_params: Mapping[str, Mapping[str, Any]],
) -> tuple[GenerationModel, ...]:
    """Merge docs-generated model params into base provider metadata."""
    generated_keys = set(generated_params)
    model_keys = {model.key for model in models}
    for missing_key in sorted(generated_keys - model_keys):
        logger.warning("Generated params reference unknown model key: %s", missing_key)

    merged_models: list[GenerationModel] = []
    fallback_model_keys: list[str] = []
    for model in models:
        generated = generated_params.get(model.key)
        if not generated:
            if model.is_enabled:
                fallback_model_keys.append(model.key)
            merged_models.append(model)
            continue

        generated_user_settings = {
            setting_key: generation_setting_from_generated(setting_key, setting_data)
            for setting_key, setting_data in dict(generated.get("user_settings", {})).items()
            if isinstance(setting_data, Mapping) and setting_key not in MEDIA_INPUT_FIELDS
        }
        generated_media_settings = sorted(set(dict(generated.get("user_settings", {}))) & MEDIA_INPUT_FIELDS)
        if generated_media_settings:
            logger.warning(
                {
                    "action": "media_input_fields_removed_from_user_settings",
                    "model_key": model.key,
                    "media_fields": generated_media_settings,
                }
            )
        user_settings = {**generated_user_settings, **model.user_settings}
        user_settings = {
            setting_key: setting
            for setting_key, setting in user_settings.items()
            if setting.is_user_visible and setting_key not in MEDIA_INPUT_FIELDS
        }

        system_settings = {**model.system_settings, **dict(generated.get("system_settings", {}))}
        replace_kwargs: dict[str, Any] = {
            "user_settings": user_settings,
            "system_settings": system_settings,
        }
        field_map = {
            "required_fields": "required_payload_fields",
            "required_payload_fields": "required_payload_fields",
            "allowed_payload_fields": "allowed_payload_fields",
            "payload_mapping": "payload_mapping",
            "pricing_rules": "pricing_rules",
            "docs_url": "docs_url",
            "endpoint": "endpoint",
            "input_media_field": "input_media_field",
            "input_requirements": "input_requirements",
            "min_images": "min_images",
            "max_images": "max_images",
            "supports_multiple_images": "supports_multiple_images",
            "requires_prompt": "requires_prompt",
            "requires_image": "requires_image",
            "requires_video": "requires_video",
            "requires_audio": "requires_audio",
            "outputs": "outputs",
            "is_enabled": "is_enabled",
            "hidden_reason": "hidden_reason",
            "warning": "warning",
        }
        for source_key, model_field in field_map.items():
            if source_key in generated:
                raw_value = generated[source_key]
                if model_field in {"required_payload_fields", "allowed_payload_fields"}:
                    replace_kwargs[model_field] = tuple(str(value) for value in raw_value)
                elif model_field == "input_requirements" and isinstance(raw_value, Mapping):
                    input_requirements = dict(raw_value)
                    prompt_requirement = input_requirements.get("prompt")
                    if isinstance(prompt_requirement, bool):
                        input_requirements["prompt"] = {"required": prompt_requirement, "payload_field": "prompt"}
                    elif not isinstance(prompt_requirement, Mapping):
                        input_requirements["prompt"] = {"required": bool(model.requires_prompt), "payload_field": "prompt"}
                    replace_kwargs[model_field] = input_requirements
                elif model_field == "payload_mapping" and isinstance(raw_value, Mapping):
                    replace_kwargs[model_field] = {str(key): str(value) for key, value in raw_value.items() if value}
                else:
                    replace_kwargs[model_field] = raw_value
        if "base_wavespeed_price_usd" in generated:
            replace_kwargs["base_wavespeed_price_usd"] = _coerce_decimal(
                generated.get("base_wavespeed_price_usd"),
                model.base_wavespeed_price_usd,
            )

        merged_model = replace(model, **replace_kwargs)
        merged_model = replace(merged_model, input_schema=build_input_schema(merged_model))
        merged_models.append(merged_model)

    if fallback_model_keys:
        logger.warning(
            "Using inferred fallback params for %s enabled model(s) without generated params: %s",
            len(fallback_model_keys),
            ", ".join(fallback_model_keys[:20]),
        )

    return tuple(merged_models)


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


def _number_setting(
    key: str,
    title: str,
    default: str,
    min_value: str,
    max_value: str,
    description: str = "",
) -> GenerationSetting:
    return GenerationSetting(
        key=key,
        title=title,
        description=description,
        type="number",
        default=default,
        options=(),
        min_value=min_value,
        max_value=max_value,
    )


def _has_only_num_generations_settings(model: GenerationModel) -> bool:
    visible_keys = {key for key, setting in model.user_settings.items() if setting.is_user_visible}
    return visible_keys <= {"num_generations"}


def _fallback_field_allowed(model: GenerationModel, field_name: str, allowed_was_missing: bool) -> bool:
    return allowed_was_missing or field_name in set(model.allowed_payload_fields)


def _resolution_from_slug(slug_text: str) -> str | None:
    for resolution in ("4k", "1080p", "720p", "480p"):
        if resolution in slug_text:
            return resolution
    return None


def _quality_values_from_slug(slug_text: str) -> tuple[str, ...]:
    values: list[str] = []
    for value, markers in (
        ("fast", ("fast",)),
        ("turbo", ("turbo",)),
        ("lite", ("lite",)),
        ("pro", ("pro",)),
        ("standard", ("std", "standard")),
        ("4k", ("4k",)),
    ):
        if any(marker in slug_text for marker in markers):
            values.append(value)
    return tuple(dict.fromkeys(values))


def _add_fallback_setting(
    settings: dict[str, GenerationSetting],
    allowed_fields: list[str],
    model: GenerationModel,
    setting: GenerationSetting,
    allowed_was_missing: bool,
) -> None:
    if setting.key in settings or not _fallback_field_allowed(model, setting.key, allowed_was_missing):
        return
    settings[setting.key] = setting
    if setting.key not in allowed_fields:
        allowed_fields.append(setting.key)


def _add_quality_or_mode_fallback(
    settings: dict[str, GenerationSetting],
    allowed_fields: list[str],
    model: GenerationModel,
    slug_text: str,
    allowed_was_missing: bool,
) -> None:
    values = _quality_values_from_slug(slug_text)
    if not values:
        return
    allowed = set(model.allowed_payload_fields)
    key = "quality" if "quality" in allowed else "mode" if "mode" in allowed else "mode"
    _add_fallback_setting(settings, allowed_fields, model, _select_setting(key, key.replace("_", " ").title(), values[0], values), allowed_was_missing)


def apply_fallback_user_settings(model: GenerationModel, *, allowed_was_missing: bool) -> GenerationModel:
    """Add conservative settings for models whose docs overlay produced only num_generations."""
    if not model.is_enabled or not _has_only_num_generations_settings(model):
        return model
    if model.generation_type in {"lipsync", "audio_to_video"}:
        return model

    settings = dict(model.user_settings)
    allowed_fields = list(model.allowed_payload_fields)
    slug_text = _model_slug_text(model)
    generation_type = model.generation_type

    if generation_type == "text_to_image":
        _add_fallback_setting(settings, allowed_fields, model, _select_setting("aspect_ratio", "Формат", "1:1", ("1:1", "16:9", "9:16", "4:3", "3:4")), allowed_was_missing)
        resolution = _resolution_from_slug(slug_text)
        if resolution:
            _add_fallback_setting(settings, allowed_fields, model, _select_setting("resolution", "Разрешение", resolution, tuple(dict.fromkeys((resolution, "1080p", "720p")))), allowed_was_missing)
        _add_fallback_setting(settings, allowed_fields, model, _text_setting("negative_prompt", "Negative Prompt", "", "Что нужно исключить из результата"), allowed_was_missing)
    elif generation_type in {"image_edit", "image_to_image"}:
        _add_fallback_setting(settings, allowed_fields, model, _number_setting("strength", "Strength", "0.5", "0.0", "1.0"), allowed_was_missing)
        _add_fallback_setting(settings, allowed_fields, model, _text_setting("negative_prompt", "Negative Prompt", "", "Что нужно исключить из результата"), allowed_was_missing)
    elif generation_type == "text_to_video":
        _add_fallback_setting(settings, allowed_fields, model, _select_setting("duration", "Длительность", "5", ("5", "10")), allowed_was_missing)
        _add_fallback_setting(settings, allowed_fields, model, _select_setting("aspect_ratio", "Формат", "16:9", ("16:9", "9:16", "1:1")), allowed_was_missing)
        _add_quality_or_mode_fallback(settings, allowed_fields, model, slug_text, allowed_was_missing)
        _add_fallback_setting(settings, allowed_fields, model, _text_setting("negative_prompt", "Negative Prompt", "", "Что нужно исключить из результата"), allowed_was_missing)
    elif generation_type in {"image_to_video", "reference_to_video"}:
        _add_fallback_setting(settings, allowed_fields, model, _select_setting("duration", "Длительность", "5", ("5", "10")), allowed_was_missing)
        _add_quality_or_mode_fallback(settings, allowed_fields, model, slug_text, allowed_was_missing)
        _add_fallback_setting(settings, allowed_fields, model, _number_setting("motion_strength", "Motion Strength", "0.5", "0.0", "1.0"), allowed_was_missing)
    elif generation_type in {"video_edit", "video_extend"}:
        if "duration" in slug_text or _fallback_field_allowed(model, "duration", False):
            _add_fallback_setting(settings, allowed_fields, model, _select_setting("duration", "Длительность", "5", ("5", "10")), allowed_was_missing)
        _add_quality_or_mode_fallback(settings, allowed_fields, model, slug_text, allowed_was_missing)

    if settings == model.user_settings:
        return model
    return replace(model, user_settings=settings, allowed_payload_fields=tuple(dict.fromkeys(allowed_fields)))


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
    payload_mapping: Optional[dict[str, str]] = None,
    input_schema: Optional[dict[str, Any]] = None,
    input_requirements: Optional[dict[str, Any]] = None,
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
        payload_mapping=payload_mapping or {},
        input_requirements=input_requirements or {},
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


def build_payload_mapping(model: GenerationModel) -> dict[str, str]:
    """Build logical input to provider payload field mapping."""
    mapping = dict(model.payload_mapping or {})
    prompt_requirement = (model.input_requirements or {}).get("prompt")
    if isinstance(prompt_requirement, Mapping) and prompt_requirement.get("payload_field"):
        mapping["prompt"] = str(prompt_requirement["payload_field"])
    elif model.requires_prompt:
        mapping.setdefault("prompt", "prompt")

    image_requirement = (model.input_requirements or {}).get("images")
    if isinstance(image_requirement, Mapping) and image_requirement.get("payload_field"):
        mapping["images"] = str(image_requirement["payload_field"])
    elif model.input_media_field in {"image", "images"} or model.requires_image:
        mapping.setdefault("images", model.input_media_field or "image")

    video_requirement = (model.input_requirements or {}).get("video")
    if isinstance(video_requirement, Mapping) and video_requirement.get("payload_field"):
        mapping["video"] = str(video_requirement["payload_field"])
    elif model.requires_video or model.input_media_field == "video":
        mapping.setdefault("video", "video")

    audio_requirement = (model.input_requirements or {}).get("audio")
    if isinstance(audio_requirement, Mapping) and audio_requirement.get("payload_field"):
        mapping["audio"] = str(audio_requirement["payload_field"])
    elif model.requires_audio:
        mapping.setdefault("audio", "audio")
    return mapping


def is_contract_complete(model: GenerationModel) -> bool:
    """Return whether a model has enough docs-derived metadata to submit safely."""
    if not model.endpoint or not model.required_payload_fields or not model.allowed_payload_fields:
        return False
    mapping = build_payload_mapping(model)
    if not mapping:
        return False
    requirements = build_input_requirements(model)
    if not isinstance(requirements.get("prompt"), Mapping):
        return False
    allowed_fields = set(model.allowed_payload_fields)
    source_fields = set(model.user_settings) | set(model.system_settings) | set(mapping.values())
    for field_name in model.required_payload_fields:
        if field_name in {"image_or_video", "text_or_audio"}:
            continue
        if field_name not in allowed_fields:
            return False
        if field_name not in source_fields:
            return False
    for input_kind in ("prompt", "images", "video", "audio"):
        requirement = requirements.get(input_kind)
        if not isinstance(requirement, Mapping) or not requirement.get("required"):
            continue
        payload_field = str(requirement.get("payload_field") or mapping.get(input_kind) or "")
        if not payload_field or payload_field not in allowed_fields:
            return False
    return True


def build_input_requirements(model: GenerationModel) -> dict[str, Any]:
    """Build declarative user input requirements separate from API settings."""
    if model.input_requirements:
        requirements = dict(model.input_requirements)
        prompt_requirement = requirements.get("prompt")
        if isinstance(prompt_requirement, bool):
            requirements["prompt"] = {"required": prompt_requirement, "payload_field": "prompt"}
        elif not isinstance(prompt_requirement, Mapping):
            requirements["prompt"] = {"required": bool(model.requires_prompt), "payload_field": "prompt"}
        mapping = dict(model.payload_mapping or {})
        for input_kind, requirement in list(requirements.items()):
            if isinstance(requirement, Mapping):
                normalized_requirement = dict(requirement)
                if not normalized_requirement.get("payload_field") and input_kind in mapping:
                    normalized_requirement["payload_field"] = mapping[input_kind]
                requirements[input_kind] = normalized_requirement
        return requirements

    mapping = dict(model.payload_mapping or {})
    requirements: dict[str, Any] = {"prompt": {"required": bool(model.requires_prompt), "payload_field": mapping.get("prompt", "prompt")}}
    if model.input_media_field in {"image", "images"} or model.requires_image:
        payload_field = model.input_media_field or ("images" if model.supports_multiple_images else "image")
        requirements["images"] = {
            "required": bool(model.requires_image),
            "min": model.min_images,
            "max": model.max_images,
            "payload_field": mapping.get("images", payload_field),
        }
    if model.input_media_field == "video" or model.requires_video:
        requirements["video"] = {
            "required": bool(model.requires_video),
            "payload_field": mapping.get("video", "video"),
        }
    if model.requires_audio:
        requirements["audio"] = {
            "required": True,
            "payload_field": mapping.get("audio", "audio"),
        }
    return requirements


def build_input_schema(model: GenerationModel) -> dict[str, Any]:
    """Собрать декларативное описание допустимых параметров модели."""
    return {
        "input_media_field": model.input_media_field,
        "min_images": model.min_images,
        "max_images": model.max_images,
        "supports_multiple_images": model.supports_multiple_images,
        "required_payload_fields": list(model.required_payload_fields),
        "allowed_payload_fields": list(model.allowed_payload_fields),
        "payload_mapping": build_payload_mapping(model),
        "input_requirements": build_input_requirements(model),
        "user_settings": {
            setting_key: {
                "type": setting.type,
                "default": setting.default,
                "options": [option.value for option in setting.options],
                "min_value": setting.min_value,
                "max_value": setting.max_value,
                "is_user_visible": setting.is_user_visible,
            }
            for setting_key, setting in model.user_settings.items()
        },
    }
