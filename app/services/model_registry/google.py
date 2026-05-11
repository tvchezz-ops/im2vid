"""Generation models for the google provider."""
from __future__ import annotations

from .base import (
    GenerationModel,
    create_wavespeed_model_from_docs_url,
)


GOOGLE_MODEL_SLUGS = (
    "google-nano-banana-pro-edit",
    "google-nano-banana-pro-edit-ultra",
    "google-nano-banana-pro-text-to-image",
    "google-nano-banana-edit",
    "google-nano-banana-text-to-image",
    "google-nano-banana-pro-text-to-image-ultra",
    "google-nano-banana-pro-edit-multi",
    "google-veo3.1-fast-image-to-video",
    "google-veo3.1-image-to-video",
    "google-gemini-2.5-flash-image-edit",
    "google-nano-banana-pro-text-to-image-multi",
    "google-veo3-image-to-video",
    "google-veo3.1-text-to-video",
    "google-imagen4",
    "google-gemini-2.5-flash-image-text-to-image",
    "google-veo3.1-reference-to-video",
    "google-veo3-fast-image-to-video",
    "google-imagen4-ultra",
    "google-veo3.1-fast-video-extend",
    "google-imagen3",
    "google-veo3.1-video-extend",
    "google-gemini-2.5-flash-image-preview-edit",
    "google-veo3-fast",
    "google-veo3",
    "google-gemini-2.5-flash-image-preview-text-to-image",
    "google-imagen4-fast",
    "google-imagen3-fast",
    "google-nano-banana-2-edit",
    "google-veo3.1-lite-image-to-video",
    "google-veo3.1-lite-start-end-to-video",
    "google-nano-banana-2-text-to-image",
    "google-nano-banana-2-text-to-image-fast",
    "google-veo2",
    "google-veo3.1-lite-text-to-video",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/google/{slug}"


PROVIDER_MODELS: list[GenerationModel] = [
    create_wavespeed_model_from_docs_url(_docs_url(slug), provider="google")
    for slug in dict.fromkeys(GOOGLE_MODEL_SLUGS)
]
