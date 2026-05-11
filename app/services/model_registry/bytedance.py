"""Generation models for the bytedance provider."""
from __future__ import annotations

from .base import (
    GenerationModel,
    create_wavespeed_model_from_docs_url,
)


BYTEDANCE_MODEL_SLUGS = (
    "bytedance-seedance-2.0-image-to-video",
    "bytedance-seedance-2.0-video-edit",
    "bytedance-seedance-2.0-video-edit-turbo",
    "bytedance-seedance-2.0-fast-video-edit-turbo",
    "bytedance-seedance-2.0-fast-video-edit",
    "bytedance-seedance-2.0-video-extend",
    "bytedance-seedance-2.0-fast-video-extend",
    "bytedance-seedance-2.0-text-to-video",
    "bytedance-seedance-2.0-fast-image-to-video",
    "bytedance-seedance-2.0-fast-text-to-video",
    "bytedance-seedance-2.0-image-to-video-turbo",
    "bytedance-seedance-2.0-text-to-video-turbo",
    "bytedance-seedance-2.0-fast-image-to-video-turbo",
    "bytedance-seedance-2.0-fast-text-to-video-turbo",
    "bytedance-seedance-v1.5-pro-image-to-video",
    "bytedance-seedance-v1.5-pro-image-to-video-fast",
    "bytedance-seedance-v1.5-pro-text-to-video",
    "bytedance-seedance-v1.5-pro-video-extend",
    "bytedance-seedance-v1.5-pro-text-to-video-fast",
    "bytedance-seedance-v1.5-pro-video-extend-fast",
    "bytedance-seedance-v1.5-pro-image-to-video-spicy",
    "bytedance-seedance-v1-pro-fast-image-to-video",
    "bytedance-seedance-v1-pro-i2v-720p",
    "bytedance-seedance-v1-pro-i2v-480p",
    "bytedance-seedance-v1-pro-i2v-1080p",
    "bytedance-seedance-v1-lite-i2v-720p",
    "bytedance-seedance-v1-lite-t2v-480p",
    "bytedance-seedance-v1-lite-i2v-480p",
    "bytedance-seedance-v1-lite-i2v-1080p",
    "bytedance-seedance-v1-pro-t2v-480p",
    "bytedance-seedance-v1-pro-fast-text-to-video",
    "bytedance-seedance-v1-lite-reference-to-video",
    "bytedance-seedance-v1-pro-t2v-720p",
    "bytedance-seedance-v1-lite-t2v-720p",
    "bytedance-seedance-v1-pro-t2v-1080p",
    "bytedance-seedance-v1-lite-t2v-1080p",
    "bytedance-seedream-v4.5-edit",
    "bytedance-seedream-v4-edit",
    "bytedance-seedream-v4.5-edit-sequential",
    "bytedance-seedream-v4.5",
    "bytedance-seedream-v4",
    "bytedance-seedream-v4-edit-sequential",
    "bytedance-seededit-v3",
    "bytedance-seedream-v4.5-sequential",
    "bytedance-seedream-v3",
    "bytedance-seedream-v4-sequential",
    "bytedance-seedream-v3.1",
    "bytedance-seedream-v5.0-lite-edit",
    "bytedance-seedream-v5.0-lite-edit-sequential",
    "bytedance-seedream-v5.0-lite",
    "bytedance-seedream-v5.0-lite-sequential",
    "bytedance-dreamina-v3.0-edit",
    "bytedance-dreamina-v3.0-pro-image-to-video",
    "bytedance-dreamina-v3.1-text-to-image",
    "bytedance-dreamina-v3.0-image-to-video-720p",
    "bytedance-dreamina-v3.0-text-to-image",
    "bytedance-dreamina-v3.0-pro-text-to-video",
    "bytedance-dreamina-v3.0-text-to-video-1080p",
    "bytedance-dreamina-v3.0-image-to-video-1080p",
    "bytedance-dreamina-v3.0-text-to-video-720p",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/bytedance/{slug}"


PROVIDER_MODELS: list[GenerationModel] = [
    create_wavespeed_model_from_docs_url(_docs_url(slug), provider="bytedance")
    for slug in dict.fromkeys(BYTEDANCE_MODEL_SLUGS)
]
