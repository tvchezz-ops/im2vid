"""Helpers for parsing Wavespeed docs pages into model metadata."""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.services.generation_service import infer_generation_type_from_endpoint


LIPSYNC_TEXT_MARKERS = (
    "lipsync",
    "talking avatar",
    "speech driven",
    "voice driven",
    "face animation",
)

NON_LIPSYNC_VIDEO_MARKERS = (
    "video edit",
    "video enhancement",
)


@dataclass(frozen=True)
class GenerationTypeDetection:
    """Detected generation type and confidence for a docs page."""

    generation_type: str
    confidence: str


def _normalize_page_text(page_content: str) -> str:
    """Collapse html/text content into a lowercase searchable string."""
    stripped_html = re.sub(r"<[^>]+>", " ", page_content or "")
    return re.sub(r"\s+", " ", stripped_html).strip().lower()


def infer_generation_type_from_page(
    page_content: str,
    endpoint: str = "",
) -> GenerationTypeDetection:
    """Infer generation type for a Wavespeed docs page."""
    endpoint_generation_type = infer_generation_type_from_endpoint(endpoint)
    if endpoint_generation_type:
        return GenerationTypeDetection(generation_type=endpoint_generation_type, confidence="high")

    normalized_text = _normalize_page_text(page_content)
    if any(marker in normalized_text for marker in LIPSYNC_TEXT_MARKERS):
        return GenerationTypeDetection(generation_type="lipsync", confidence="medium")

    if any(marker in normalized_text for marker in NON_LIPSYNC_VIDEO_MARKERS):
        return GenerationTypeDetection(generation_type="", confidence="")

    return GenerationTypeDetection(generation_type="", confidence="")