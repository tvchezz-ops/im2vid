#!/usr/bin/env python3
"""Sync Wavespeed docs request parameters into generated_params.py."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from pprint import pformat
import ssl
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.model_docs_parser import ModelDocsField, parse_model_docs
from app.services.model_registry import list_generation_models
from app.services.model_registry.generated_params import GENERATED_MODEL_PARAMS
from app.utils import logger


OUTPUT_PATH = ROOT / "app" / "services" / "model_registry" / "generated_params.py"
INTERNAL_FIELDS = {
    "seed",
    "webhook_url",
    "callback_url",
    "enable_prompt_expansion",
    "enable_sync_mode",
    "enable_base64_output",
    "num_generations",
    "num_outputs",
}
SYSTEM_BOOLEAN_FIELDS = {"enable_sync_mode", "enable_base64_output", "enable_prompt_expansion"}
MEDIA_INPUT_FIELDS = {
    "image",
    "images",
    "image_url",
    "image_urls",
    "input_image",
    "input_images",
    "video",
    "video_url",
    "input_video",
    "audio",
    "audio_url",
    "input_audio",
    "first_frame",
    "last_frame",
    "reference_image",
    "reference_images",
    "face_image",
    "source_image",
    "target_image",
}
REQUIRED_MEDIA_FIELDS = {"prompt", "text", "image_or_video", "text_or_audio", *MEDIA_INPUT_FIELDS}


def fetch_docs(url: str) -> str:
    request = Request(url, headers={"User-Agent": "im2vid-model-param-sync/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except (ssl.SSLError, URLError) as exc:
        if not isinstance(getattr(exc, "reason", exc), ssl.SSLError):
            raise
        with urlopen(request, timeout=30, context=ssl._create_unverified_context()) as response:
            return response.read().decode("utf-8", errors="replace")


def _field_to_setting(field: ModelDocsField) -> dict[str, Any]:
    setting_type = field.field_type
    if field.enum_options:
        setting_type = "enum"
    setting: dict[str, Any] = {
        "title": field.name.replace("_", " ").title(),
        "type": setting_type,
    }
    if field.default is not None:
        setting["default"] = field.default
    if field.enum_options:
        setting["options"] = list(field.enum_options)
    if field.min_value is not None:
        setting["min_value"] = field.min_value
    if field.max_value is not None:
        setting["max_value"] = field.max_value
    if field.name == "negative_prompt":
        setting["description"] = "Что нужно исключить из результата"
    elif field.description:
        setting["description"] = field.description
    return setting


def _is_user_visible_field(field: ModelDocsField) -> bool:
    if field.name in INTERNAL_FIELDS:
        return False
    if field.name in REQUIRED_MEDIA_FIELDS:
        return False
    return True


def _first_present_field(fields: list[ModelDocsField], candidates: tuple[str, ...]) -> str | None:
    field_names = {field.name for field in fields}
    for candidate in candidates:
        if candidate in field_names:
            return candidate
    return None


def _build_input_requirements(model: Any, fields: list[ModelDocsField]) -> dict[str, Any]:
    field_by_name = {field.name: field for field in fields}
    required_names = {field.name for field in fields if field.required}
    requirements: dict[str, Any] = {
        "prompt": {
            "required": "prompt" in required_names or bool(getattr(model, "requires_prompt", False)),
            "payload_field": "prompt",
        }
    }

    image_field = _first_present_field(
        fields,
        (
            "images",
            "image_urls",
            "input_images",
            "reference_images",
            "image",
            "image_url",
            "input_image",
            "reference_image",
            "first_frame",
            "face_image",
            "source_image",
            "target_image",
        ),
    )
    if image_field:
        is_multi = image_field in {"images", "image_urls", "input_images", "reference_images"}
        requirements["images"] = {
            "required": image_field in required_names,
            "min": int(getattr(model, "min_images", 0) or (1 if image_field in required_names else 0)),
            "max": int(getattr(model, "max_images", 0) or (10 if is_multi else 1)),
            "payload_field": image_field,
        }

    video_field = _first_present_field(fields, ("video", "video_url", "input_video"))
    if video_field:
        requirements["video"] = {"required": video_field in required_names, "payload_field": video_field}

    audio_field = _first_present_field(fields, ("audio", "audio_url", "input_audio"))
    if audio_field:
        requirement: dict[str, Any] = {"required": audio_field in required_names, "payload_field": audio_field}
        description = (field_by_name[audio_field].description or "").lower()
        if "5mb" in description or "5 mb" in description:
            requirement["max_size_mb"] = 5
        requirements["audio"] = requirement
    return requirements


def _generated_entry(model: Any, page_content: str, synced_at: str) -> dict[str, Any]:
    existing_entry = dict(GENERATED_MODEL_PARAMS.get(model.key, {}))
    schema = parse_model_docs(page_content)
    fields = list(schema.fields)
    required_fields: list[str] = []
    allowed_fields: list[str] = []
    user_settings: dict[str, dict[str, Any]] = {}
    system_settings: dict[str, Any] = {}

    for field in fields:
        if field.name in INTERNAL_FIELDS:
            continue
        if field.name in SYSTEM_BOOLEAN_FIELDS:
            allowed_fields.append(field.name)
            system_settings[field.name] = bool(field.default) if field.default is not None else False
            continue
        allowed_fields.append(field.name)
        if field.required:
            required_fields.append(field.name)
        if _is_user_visible_field(field):
            user_settings[field.name] = _field_to_setting(field)

    if not required_fields:
        required_fields = list(model.required_payload_fields)
    if not allowed_fields:
        allowed_fields = list(model.allowed_payload_fields)
    input_requirements = _build_input_requirements(model, fields)
    media_fields = [field.name for field in fields if field.name in MEDIA_INPUT_FIELDS]
    logger.info(
        {
            "action": "generated_params_media_fields_separated",
            "model_key": model.key,
            "media_fields": media_fields,
            "user_settings_count": len(user_settings),
            "input_requirements": input_requirements,
        }
    )

    preserved_keys = {
        key: existing_entry[key]
        for key in (
            "base_wavespeed_price_usd",
            "pricing_rules",
            "input_media_field",
            "min_images",
            "max_images",
            "supports_multiple_images",
            "requires_prompt",
            "requires_image",
            "requires_video",
            "requires_audio",
            "outputs",
        )
        if key in existing_entry
    }
    return {
        **preserved_keys,
        "allowed_payload_fields": sorted(dict.fromkeys(allowed_fields)),
        "docs_url": model.docs_url,
        "last_synced_at": synced_at,
        "required_fields": sorted(dict.fromkeys(required_fields)),
        "input_requirements": input_requirements,
        "system_settings": dict(sorted(system_settings.items())),
        "user_settings": dict(sorted(user_settings.items())),
    }


def _debug_entry(model: Any, entry: dict[str, Any], skipped_internal_fields: list[str]) -> None:
    user_settings = dict(entry.get("user_settings", {}))
    print(f"docs_url: {model.docs_url}")
    print(f"extracted fields: {', '.join(entry.get('allowed_payload_fields', []))}")
    print(f"required fields: {', '.join(entry.get('required_fields', []))}")
    print(f"user_visible fields: {', '.join(user_settings)}")
    print(f"skipped internal fields: {', '.join(skipped_internal_fields)}")
    print(f"input_requirements: {entry.get('input_requirements', {})}")
    print(f"generated user_settings count: {len(user_settings)}")


def render_generated_params(entries: dict[str, dict[str, Any]]) -> str:
    return (
        '"""Generated Wavespeed model parameter metadata.\n\n'
        'This file is maintained by scripts/sync_wavespeed_model_params.py.\n'
        'Runtime code imports this static snapshot only; it must not perform network I/O.\n'
        '"""\n'
        "from __future__ import annotations\n\n\n"
        f"GENERATED_MODEL_PARAMS = {pformat(entries, width=120, sort_dicts=True)}\n"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Wavespeed docs request parameters into generated_params.py")
    parser.add_argument("--only", action="append", default=[], help="Model key to sync. Can be repeated or comma-separated.")
    parser.add_argument("--debug", action="store_true", help="Print extraction details for synced models.")
    return parser.parse_args()


def _selected_model_keys(raw_values: list[str]) -> set[str]:
    keys: set[str] = set()
    for raw_value in raw_values:
        keys.update(key.strip() for key in raw_value.split(",") if key.strip())
    return keys


def main() -> None:
    args = parse_args()
    selected_keys = _selected_model_keys(args.only)
    synced_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    entries: dict[str, dict[str, Any]] = dict(GENERATED_MODEL_PARAMS) if selected_keys else {}
    failed_extractions: list[str] = []
    models = [
        model
        for model in sorted(list_generation_models(), key=lambda item: item.key)
        if not selected_keys or model.key in selected_keys
    ]
    missing_keys = selected_keys - {model.key for model in models}
    for missing_key in sorted(missing_keys):
        print(f"WARN: unknown model key requested: {missing_key}", file=sys.stderr)
        failed_extractions.append(missing_key)

    for model in models:
        if not model.docs_url:
            continue
        print(f"Fetching {model.key}: {model.docs_url}")
        try:
            page_content = fetch_docs(model.docs_url)
            entry = _generated_entry(model, page_content, synced_at)
            parsed_fields = {field.name for field in parse_model_docs(page_content).fields}
            skipped_internal_fields = sorted(parsed_fields & INTERNAL_FIELDS)
            if not entry.get("user_settings") and not entry.get("input_requirements"):
                print(f"WARN: no_docs_params_extracted model_key={model.key}", file=sys.stderr)
                failed_extractions.append(model.key)
            entries[model.key] = entry
            if args.debug:
                _debug_entry(model, entry, skipped_internal_fields)
        except Exception as exc:
            print(f"WARN: failed to sync {model.key}: {exc}", file=sys.stderr)
            failed_extractions.append(model.key)

    OUTPUT_PATH.write_text(render_generated_params(entries), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)} with {len(entries)} model entries")
    if failed_extractions:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
