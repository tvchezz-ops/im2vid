"""Tests for the Wavespeed docs parser scaffold."""

from __future__ import annotations

import os

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from scripts.parse_wavespeed_docs import (
    extract_default_values,
    extract_enum_options,
    extract_field_types,
    extract_optional_fields,
    extract_required_fields,
    infer_generation_type_from_page,
    parse_model_docs,
)


@pytest.mark.parametrize(
    "page_content",
    [
        "Advanced lipsync workflow for video avatars",
        "Create a talking avatar from a single portrait",
        "Speech driven face synthesis for presenters",
        "Voice driven avatar animation pipeline",
        "Face animation model for realistic dialogue clips",
    ],
)
def test_infer_generation_type_from_page_detects_lipsync_by_text(page_content: str) -> None:
    detection = infer_generation_type_from_page(page_content)

    assert detection.generation_type == "lipsync"
    assert detection.confidence == "medium"


def test_infer_generation_type_from_page_does_not_confuse_video_edit_with_lipsync() -> None:
    detection = infer_generation_type_from_page("Professional video edit and video enhancement model")

    assert detection.generation_type == ""
    assert detection.confidence == ""


def test_infer_generation_type_from_page_prefers_endpoint_detection_when_available() -> None:
    detection = infer_generation_type_from_page(
        "Talking avatar and face animation model",
        endpoint="https://wavespeed.ai/docs-api/google/video-to-video/model",
    )

    assert detection.generation_type == "video_edit"
    assert detection.confidence == "high"


def test_parse_model_docs_extracts_json_schema_fields() -> None:
    schema = parse_model_docs(
        '{"type":"object","required":["prompt"],"properties":{'
        '"prompt":{"type":"string"},'
        '"duration":{"type":"integer","default":5,"minimum":1,"maximum":8},'
        '"aspect_ratio":{"type":"string","enum":["16:9","9:16"],"default":"16:9"}'
        '}}'
    )

    assert extract_required_fields(schema) == ("prompt",)
    assert "duration" in extract_optional_fields(schema)
    assert extract_enum_options(schema)["aspect_ratio"] == ("16:9", "9:16")
    assert extract_default_values(schema)["duration"] == 5
    assert extract_field_types(schema)["duration"] == "integer"
