#!/usr/bin/env python3
"""Sync Wavespeed docs request parameters into generated_params.py."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import html
from pathlib import Path
from pprint import pformat
import re
import ssl
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.model_docs_parser import ModelDocsField, parse_model_docs
from app.services.model_registry import ALL_GENERATION_MODELS, apply_generated_model_params, build_model_registry, is_contract_complete, normalize_model_key
from app.services.model_registry.generated_params import GENERATED_MODEL_PARAMS


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
REQUIRED_MEDIA_FIELDS = {"prompt", "text", "image_or_video", "text_or_audio", *MEDIA_INPUT_FIELDS}
USER_SETTING_ALLOWLIST = {
    "duration",
    "resolution",
    "size",
    "aspect_ratio",
    "quality",
    "mode",
    "negative_prompt",
    "strength",
    "guidance_scale",
    "guidance",
    "fps",
    "motion_strength",
    "camera_control",
    "output_format",
    "watermark",
    "style",
    "shot_type",
    "enable_audio",
}
ENDPOINT_RE = re.compile(r'https://api\.wavespeed\.ai/api/v3/[^"\s\\<&]+')


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
    return field.name in USER_SETTING_ALLOWLIST


def _extract_endpoint(page_content: str, fallback_endpoint: str) -> str:
    match = ENDPOINT_RE.search(html.unescape(page_content or ""))
    if match:
        return match.group(0).rstrip("'")
    return fallback_endpoint


def _endpoint_matches_model(endpoint: str, model_key: str) -> bool:
    endpoint_key = normalize_model_key(endpoint.rstrip("/").rsplit("/api/v3/", 1)[-1])
    return endpoint_key.endswith(model_key) or model_key.endswith(endpoint_key)


def _extract_file_types(description: str) -> list[str]:
    return sorted(set(re.findall(r"\.(?:mp3|wav|m4a|aac|mp4|mov|webm|avi|mkv|m4v|jpg|jpeg|png|webp)", description or "", flags=re.IGNORECASE)))


def _build_payload_mapping(input_requirements: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for input_kind in ("prompt", "images", "video", "audio"):
        requirement = input_requirements.get(input_kind)
        if isinstance(requirement, dict) and requirement.get("payload_field"):
            mapping[input_kind] = str(requirement["payload_field"])
    return mapping


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
            "reference_urls",
            "image",
            "image_url",
            "input_image",
            "reference_image",
            "reference_url",
            "first_frame",
            "first_image",
            "start_image",
            "last_frame",
            "last_image",
            "end_image",
            "face_image",
            "source_image",
            "target_image",
            "element_refer_list",
        ),
    )
    if image_field:
        is_multi = image_field in {"images", "image_urls", "input_images", "reference_images", "reference_urls", "element_refer_list"}
        requirements["images"] = {
            "required": image_field in required_names,
            "min": int(getattr(model, "min_images", 0) or (1 if image_field in required_names else 0)),
            "max": int((getattr(model, "max_images", 0) if is_multi else 0) or (10 if is_multi else 1)),
            "payload_field": image_field,
        }

    video_field = _first_present_field(fields, ("video", "video_url", "input_video", "videos", "video_urls"))
    if video_field:
        requirements["video"] = {"required": video_field in required_names, "payload_field": video_field}

    audio_field = _first_present_field(fields, ("audio", "audio_url", "input_audio"))
    if audio_field:
        requirement: dict[str, Any] = {"required": audio_field in required_names, "payload_field": audio_field}
        description = (field_by_name[audio_field].description or "").lower()
        if "5mb" in description or "5 mb" in description:
            requirement["max_size_mb"] = 5
        file_types = _extract_file_types(description)
        if file_types:
            requirement["file_types"] = file_types
        requirements["audio"] = requirement
    return requirements


def _generated_entry(model: Any, page_content: str, synced_at: str) -> dict[str, Any]:
    existing_entry = dict(GENERATED_MODEL_PARAMS.get(model.key, {}))
    schema = parse_model_docs(page_content)
    fields = list(schema.fields)
    if not fields:
        raise ValueError("no request fields parsed from docs")
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
    payload_mapping = _build_payload_mapping(input_requirements)
    endpoint = _extract_endpoint(page_content, model.endpoint)
    if not _endpoint_matches_model(endpoint, model.key):
        endpoint = model.endpoint

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
        "endpoint": endpoint,
        "allowed_payload_fields": sorted(dict.fromkeys(allowed_fields)),
        "docs_url": model.docs_url,
        "last_synced_at": synced_at,
        "payload_mapping": payload_mapping,
        "required_fields": sorted(dict.fromkeys(required_fields)),
        "input_requirements": input_requirements,
        "system_settings": dict(sorted(system_settings.items())),
        "user_settings": dict(sorted(user_settings.items())),
    }


def _disabled_entry(model: Any, synced_at: str, reason: str) -> dict[str, Any]:
    return {
        "docs_url": model.docs_url,
        "endpoint": model.endpoint,
        "hidden_reason": reason,
        "is_enabled": False,
        "last_synced_at": synced_at,
        "warning": "Model disabled because docs contract could not be parsed",
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
    parser.add_argument("--all", action="store_true", help="Sync every enabled base model with docs_url.")
    parser.add_argument("--only", action="append", default=[], help="Model key to sync. Can be repeated or comma-separated.")
    parser.add_argument("--debug", action="store_true", help="Print extraction details for synced models.")
    parser.add_argument("--workers", type=int, default=12, help="Maximum concurrent docs fetches for --all sync.")
    parser.add_argument("--fail-under-coverage", type=float, default=0.6, help="Fail if docs params coverage is below this ratio.")
    parser.add_argument("--allow-low-coverage", action="store_true", help="Do not fail when docs params coverage is below threshold.")
    return parser.parse_args()


def _selected_model_keys(raw_values: list[str]) -> set[str]:
    keys: set[str] = set()
    for raw_value in raw_values:
        keys.update(key.strip() for key in raw_value.split(",") if key.strip())
    return keys


def _sync_one_model(model: Any, synced_at: str) -> tuple[Any, dict[str, Any], set[str], list[str], Exception | None]:
    try:
        page_content = fetch_docs(model.docs_url)
        entry = _generated_entry(model, page_content, synced_at)
        parsed_fields = {field.name for field in parse_model_docs(page_content).fields}
        skipped_internal_fields = sorted(parsed_fields & INTERNAL_FIELDS)
        return model, entry, parsed_fields, skipped_internal_fields, None
    except Exception as exc:
        return model, _disabled_entry(model, synced_at, "missing_docs_contract"), set(), [], exc


def main() -> None:
    args = parse_args()
    selected_keys = _selected_model_keys(args.only)
    synced_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    entries: dict[str, dict[str, Any]] = dict(GENERATED_MODEL_PARAMS) if selected_keys else {}
    failed_extractions: list[str] = []
    parsed_successfully: set[str] = set()
    docs_params_keys: set[str] = set()
    base_models = [model for model in ALL_GENERATION_MODELS if model.is_enabled]
    models = [
        model
        for model in sorted(base_models, key=lambda item: item.key)
        if not selected_keys or model.key in selected_keys
    ]
    missing_keys = selected_keys - {model.key for model in models}
    for missing_key in sorted(missing_keys):
        print(f"WARN: unknown model key requested: {missing_key}", file=sys.stderr)
        failed_extractions.append(missing_key)

    docs_models = [model for model in models if model.docs_url]
    workers = max(1, min(int(args.workers), len(docs_models) or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_by_key = {
            executor.submit(_sync_one_model, model, synced_at): model.key
            for model in docs_models
        }
        for future in as_completed(future_by_key):
            model, entry, parsed_fields, skipped_internal_fields, exc = future.result()
            if exc is None:
                print(f"Parsed {model.key}: {model.docs_url}")
                if not entry.get("user_settings") and not entry.get("input_requirements"):
                    print(f"WARN: no_docs_params_extracted model_key={model.key}", file=sys.stderr)
                    failed_extractions.append(model.key)
                    entry = _disabled_entry(model, synced_at, "missing_docs_contract")
                else:
                    parsed_successfully.add(model.key)
                if len(dict(entry.get("user_settings", {}))) > 0:
                    docs_params_keys.add(model.key)
                entries[model.key] = entry
                if args.debug:
                    _debug_entry(model, entry, skipped_internal_fields)
            else:
                print(f"WARN: failed to sync {model.key}: {exc}", file=sys.stderr)
                failed_extractions.append(model.key)
                entries[model.key] = entry

    OUTPUT_PATH.write_text(render_generated_params(entries), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)} with {len(entries)} model entries")
    total_models = len(models)
    synced_entries = [dict(entries.get(model.key, {})) for model in models]
    models_with_more_than_one_user_setting = sum(1 for entry in synced_entries if len(dict(entry.get("user_settings", {}))) > 1)
    models_with_only_fallback = total_models - len(docs_params_keys)
    coverage = (len(docs_params_keys) / total_models) if total_models else 1.0
    synced_model_keys = {model.key for model in models}
    synced_base_models = tuple(model for model in ALL_GENERATION_MODELS if model.key in synced_model_keys)
    synced_registry = build_model_registry(apply_generated_model_params(synced_base_models, entries))
    full_contract_count = sum(1 for model in synced_registry.values() if model.is_enabled and is_contract_complete(model))
    disabled_contract_count = sum(
        1
        for entry in synced_entries
        if entry.get("is_enabled") is False and entry.get("hidden_reason") == "missing_docs_contract"
    )
    print(f"Total models: {total_models}")
    print(f"Models parsed successfully: {len(parsed_successfully)}")
    print(f"Parsed ok: {len(parsed_successfully)}")
    print(f"Parsed failed: {len(failed_extractions)}")
    print(f"Models with full contract: {full_contract_count}")
    print(f"Models disabled because contract missing: {disabled_contract_count}")
    print(f"Models with >1 user setting: {models_with_more_than_one_user_setting}")
    print(f"Models with only fallback: {models_with_only_fallback}")
    print(f"Failed docs: {len(failed_extractions)}")
    print(f"Sample failed keys: {', '.join(failed_extractions[:20])}")
    print(f"Docs params coverage: {coverage:.3f}")
    if failed_extractions and not args.allow_low_coverage:
        raise SystemExit(1)
    if not args.allow_low_coverage and coverage < args.fail_under_coverage:
        print(
            f"ERROR: docs params coverage {coverage:.3f} below threshold {args.fail_under_coverage:.3f}; "
            "rerun with --allow-low-coverage to write anyway.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
