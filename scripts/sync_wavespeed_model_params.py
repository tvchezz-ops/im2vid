#!/usr/bin/env python3
"""Sync Wavespeed docs request parameters into generated_params.py."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from pprint import pformat
import sys
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.model_docs_parser import ModelDocsField, parse_model_docs
from app.services.model_registry import list_generation_models
from app.services.model_registry.generated_params import GENERATED_MODEL_PARAMS


OUTPUT_PATH = ROOT / "app" / "services" / "model_registry" / "generated_params.py"
INTERNAL_FIELDS = {
    "seed",
    "enable_prompt_expansion",
    "num_generations",
    "num_outputs",
}
SYSTEM_BOOLEAN_FIELDS = {"enable_sync_mode", "enable_base64_output"}
MEDIA_FIELDS = {"prompt", "image", "images", "video", "audio", "text", "image_or_video", "text_or_audio"}


def fetch_docs(url: str) -> str:
    request = Request(url, headers={"User-Agent": "im2vid-model-param-sync/1.0"})
    with urlopen(request, timeout=30) as response:
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
    if field.description:
        setting["description"] = field.description
    return setting


def _generated_entry(model: Any, page_content: str, synced_at: str) -> dict[str, Any]:
    existing_entry = dict(GENERATED_MODEL_PARAMS.get(model.key, {}))
    schema = parse_model_docs(page_content)
    required_fields: list[str] = []
    allowed_fields: list[str] = []
    user_settings: dict[str, dict[str, Any]] = {}
    system_settings: dict[str, Any] = {}

    for field in schema.fields:
        if field.name in INTERNAL_FIELDS:
            continue
        if field.name in SYSTEM_BOOLEAN_FIELDS:
            allowed_fields.append(field.name)
            system_settings[field.name] = bool(field.default) if field.default is not None else False
            continue
        allowed_fields.append(field.name)
        if field.required:
            required_fields.append(field.name)
        if field.name not in MEDIA_FIELDS:
            user_settings[field.name] = _field_to_setting(field)

    if not required_fields:
        required_fields = list(model.required_payload_fields)
    if not allowed_fields:
        allowed_fields = list(model.allowed_payload_fields)

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
        "system_settings": dict(sorted(system_settings.items())),
        "user_settings": dict(sorted(user_settings.items())),
    }


def render_generated_params(entries: dict[str, dict[str, Any]]) -> str:
    return (
        '"""Generated Wavespeed model parameter metadata.\n\n'
        'This file is maintained by scripts/sync_wavespeed_model_params.py.\n'
        'Runtime code imports this static snapshot only; it must not perform network I/O.\n'
        '"""\n'
        "from __future__ import annotations\n\n\n"
        f"GENERATED_MODEL_PARAMS = {pformat(entries, width=120, sort_dicts=True)}\n"
    )


def main() -> None:
    synced_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    entries: dict[str, dict[str, Any]] = {}
    for model in sorted(list_generation_models(), key=lambda item: item.key):
        if not model.docs_url:
            continue
        print(f"Fetching {model.key}: {model.docs_url}")
        try:
            page_content = fetch_docs(model.docs_url)
            entries[model.key] = _generated_entry(model, page_content, synced_at)
        except Exception as exc:
            print(f"WARN: failed to sync {model.key}: {exc}", file=sys.stderr)

    OUTPUT_PATH.write_text(render_generated_params(entries), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)} with {len(entries)} model entries")


if __name__ == "__main__":
    main()
