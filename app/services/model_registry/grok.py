"""Generation models for the grok provider."""
from __future__ import annotations

from .base import GenerationModel, create_wavespeed_model_from_docs_url


GROK_MODEL_SLUGS = (
    "x-ai-grok-imagine-video-image-to-video",
    "x-ai-grok-imagine-image-edit",
    "x-ai-grok-imagine-image-text-to-image",
    "x-ai-grok-2-image",
    "x-ai-grok-imagine-video-text-to-video",
    "x-ai-grok-imagine-video-edit-video",
    "x-ai-grok-imagine-video-reference-to-video",
    "x-ai-grok-imagine-video-video-extend",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/x-ai/{slug}"


PROVIDER_MODELS: list[GenerationModel] = [
    create_wavespeed_model_from_docs_url(_docs_url(slug), provider="grok")
    for slug in dict.fromkeys(GROK_MODEL_SLUGS)
]
