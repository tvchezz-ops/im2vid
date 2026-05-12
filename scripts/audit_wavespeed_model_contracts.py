#!/usr/bin/env python3
"""Audit generated Wavespeed model contracts for safe submission."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.generation_service import build_payload
from app.services.model_registry import is_contract_complete, list_generation_models
from app.services.model_registry.base import MEDIA_INPUT_FIELDS


def _mock_inputs(model: Any) -> tuple[list[str], str, dict[str, Any]]:
    prompt = "A clean production test prompt" if model.requires_prompt else ""
    media_urls: list[str] = []
    settings: dict[str, Any] = {}
    requirements = model.input_requirements or {}
    image_requirement = requirements.get("images") if isinstance(requirements.get("images"), dict) else {}
    min_images = int(image_requirement.get("min") or model.min_images or 1)
    if model.input_media_field in {"image", "images"} or model.requires_image:
        media_urls = [f"https://example.com/input-{index}.png" for index in range(max(1, min_images))]
    if model.input_media_field == "video" or model.requires_video:
        settings["input_video_url"] = "https://example.com/input.mp4"
    if model.requires_audio:
        settings["input_audio_url"] = "https://example.com/input.mp3"
    return media_urls, prompt, settings


def audit_model(model: Any) -> list[str]:
    issues: list[str] = []
    if not model.endpoint:
        issues.append("missing endpoint")
    if not model.required_payload_fields:
        issues.append("missing required_fields")
    if not model.allowed_payload_fields:
        issues.append("missing allowed_payload_fields")
    if not model.payload_mapping:
        issues.append("missing payload_mapping")
    media_settings = sorted(set(model.user_settings) & set(MEDIA_INPUT_FIELDS))
    if media_settings:
        issues.append(f"media fields in user_settings: {media_settings}")
    if not is_contract_complete(model):
        issues.append("incomplete generated contract")

    requirements = model.input_requirements or {}
    if model.generation_type in {"text_to_image", "text_to_video"}:
        for input_kind in ("images", "video", "audio"):
            requirement = requirements.get(input_kind)
            if isinstance(requirement, dict) and requirement.get("required"):
                issues.append(f"text model unexpectedly requires {input_kind}")
    if model.generation_type in {"image_to_video", "image_edit", "image_to_image", "reference_to_video"}:
        if not isinstance(requirements.get("images"), dict) or not requirements["images"].get("required"):
            issues.append("image model does not require image input")
    if model.generation_type in {"video_edit", "video_extend", "video_to_audio"}:
        if not isinstance(requirements.get("video"), dict) or not requirements["video"].get("required"):
            issues.append("video model does not require video input")

    try:
        media_urls, prompt, settings = _mock_inputs(model)
        build_payload(model.key, media_urls, prompt, settings)
    except Exception as exc:
        issues.append(f"build_payload failed with mock inputs: {exc}")
    return issues


def main() -> None:
    problems: dict[str, list[str]] = {}
    models = list_generation_models()
    for model in models:
        issues = audit_model(model)
        if issues:
            problems[model.key] = issues

    if problems:
        print("Invalid Wavespeed model contracts:")
        for model_key, issues in problems.items():
            print(f"- {model_key}: {'; '.join(issues)}")
        raise SystemExit(1)
    print(f"Audit passed for {len(models)} enabled model(s).")


if __name__ == "__main__":
    main()
