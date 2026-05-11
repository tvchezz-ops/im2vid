"""Generation models for the openai provider."""
from __future__ import annotations

from .base import GenerationModel, create_wavespeed_model_from_docs_url


OPENAI_MODEL_SLUGS = (
    "openai-sora-2-image-to-video",
    "openai-gpt-image-1.5-edit",
    "openai-sora-2-text-to-video",
    "openai-sora-2-image-to-video-pro",
    "openai-gpt-image-1.5-text-to-image",
    "openai-sora-2-text-to-video-pro",
    "openai-gpt-image-1-mini-edit",
    "openai-gpt-image-1-text-to-image",
    "openai-gpt-image-1",
    "openai-gpt-image-1-mini-text-to-image",
    "openai-gpt-image-1-high-fidelity",
    "openai-dall-e-3",
    "openai-sora",
    "openai-dall-e-2",
    "openai-gpt-image-2-edit",
    "openai-gpt-image-2-text-to-image",
    "openai-sora-2-pro-image-to-video",
    "openai-sora-2-pro-text-to-video",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/openai/{slug}"


PROVIDER_MODELS: list[GenerationModel] = [
    create_wavespeed_model_from_docs_url(_docs_url(slug), provider="openai")
    for slug in dict.fromkeys(OPENAI_MODEL_SLUGS)
]
