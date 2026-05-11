"""Generation models for the alibaba provider."""
from __future__ import annotations

from .base import GenerationModel, create_wavespeed_model_from_docs_url


ALIBABA_MODEL_SLUGS = (
    "alibaba-wan-2.7-image-to-video",
    "alibaba-wan-2.7-image-edit",
    "alibaba-wan-2.7-video-edit",
    "alibaba-wan-2.7-image-edit-pro",
    "alibaba-wan-2.7-text-to-video",
    "alibaba-wan-2.7-reference-to-video",
    "alibaba-wan-2.7-video-extend",
    "alibaba-wan-2.7-text-to-image-pro",
    "alibaba-wan-2.7-text-to-image",
    "alibaba-happyhorse-1.0-text-to-video",
    "alibaba-happyhorse-1.0-video-extend",
    "alibaba-happyhorse-1.0-video-edit",
    "alibaba-happyhorse-1.0-reference-to-video",
    "alibaba-happyhorse-1.0-image-to-video",
    "alibaba-wan-2.6-image-to-video",
    "alibaba-wan-2.6-image-edit",
    "alibaba-wan-2.6-image-to-video-flash",
    "alibaba-wan-2.6-text-to-image",
    "alibaba-wan-2.6-text-to-video",
    "alibaba-wan-2.6-reference-to-video-flash",
    "alibaba-wan-2.6-reference-to-video",
    "alibaba-wan-2.6-image-to-video-spicy",
    "alibaba-wan-2.6-video-extend",
    "alibaba-wan-2.6-image-to-video-pro",
    "alibaba-wan-2.2-i2v-plus-1080p",
    "alibaba-wan-2.2-i2v-plus-480p",
    "alibaba-wan-2.2-t2v-plus-1080p",
    "alibaba-wan-2.2-t2v-plus-480p",
    "alibaba-wan-2.5-image-to-video",
    "alibaba-wan-2.5-video-extend",
    "alibaba-wan-2.5-image-edit",
    "alibaba-wan-2.5-text-to-video",
    "alibaba-wan-2.5-text-to-image",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/alibaba/{slug}"


PROVIDER_MODELS: list[GenerationModel] = [
    create_wavespeed_model_from_docs_url(_docs_url(slug), provider="alibaba")
    for slug in dict.fromkeys(ALIBABA_MODEL_SLUGS)
]
