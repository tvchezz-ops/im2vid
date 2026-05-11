"""Generation models for the kling provider."""
from __future__ import annotations

from .base import GenerationModel, create_wavespeed_model_from_docs_url


KLING_MODEL_SLUGS = (
    "kwaivgi-kling-effects",
    "kwaivgi-kling-elements",
    "kwaivgi-kling-elements-advanced",
    "kwaivgi-kling-image-o1",
    "kwaivgi-kling-image-o3-edit",
    "kwaivgi-kling-image-o3-text-to-image",
    "kwaivgi-kling-image-v3-edit",
    "kwaivgi-kling-image-v3-text-to-image",
    "kwaivgi-kling-lipsync-audio-to-video",
    "kwaivgi-kling-lipsync-text-to-video",
    "kwaivgi-kling-v1-ai-avatar-pro",
    "kwaivgi-kling-v1-ai-avatar-standard",
    "kwaivgi-kling-v1-ai-multi-shot",
    "kwaivgi-kling-v1.6-i2v-pro",
    "kwaivgi-kling-v1.6-i2v-standard",
    "kwaivgi-kling-v1.6-multi-i2v-pro",
    "kwaivgi-kling-v1.6-multi-i2v-standard",
    "kwaivgi-kling-v1.6-t2v-standard",
    "kwaivgi-kling-v2-ai-avatar-pro",
    "kwaivgi-kling-v2-ai-avatar-standard",
    "kwaivgi-kling-v2.0-i2v-master",
    "kwaivgi-kling-v2.0-t2v-master",
    "kwaivgi-kling-v2.1-i2v-master",
    "kwaivgi-kling-v2.1-i2v-pro",
    "kwaivgi-kling-v2.1-i2v-pro-start-end-frame",
    "kwaivgi-kling-v2.1-i2v-standard",
    "kwaivgi-kling-v2.1-t2v-master",
    "kwaivgi-kling-v2.5-turbo-pro-image-to-video",
    "kwaivgi-kling-v2.5-turbo-pro-text-to-video",
    "kwaivgi-kling-v2.5-turbo-std-image-to-video",
    "kwaivgi-kling-v2.6-pro-image-to-video",
    "kwaivgi-kling-v2.6-pro-motion-control",
    "kwaivgi-kling-v2.6-pro-text-to-video",
    "kwaivgi-kling-v2.6-std-image-to-video",
    "kwaivgi-kling-v2.6-std-motion-control",
    "kwaivgi-kling-v2.6-std-text-to-video",
    "kwaivgi-kling-v3.0-4k-image-to-video",
    "kwaivgi-kling-v3.0-4k-text-to-video",
    "kwaivgi-kling-v3.0-pro-image-to-video",
    "kwaivgi-kling-v3.0-pro-motion-control",
    "kwaivgi-kling-v3.0-pro-text-to-video",
    "kwaivgi-kling-v3.0-std-image-to-video",
    "kwaivgi-kling-v3.0-std-motion-control",
    "kwaivgi-kling-v3.0-std-text-to-video",
    "kwaivgi-kling-video-o1-image-to-video",
    "kwaivgi-kling-video-o1-reference-to-video",
    "kwaivgi-kling-video-o1-std-image-to-video",
    "kwaivgi-kling-video-o1-std-reference-to-video",
    "kwaivgi-kling-video-o1-std-text-to-video",
    "kwaivgi-kling-video-o1-std-video-edit",
    "kwaivgi-kling-video-o1-text-to-video",
    "kwaivgi-kling-video-o1-video-edit",
    "kwaivgi-kling-video-o1-video-edit-fast",
    "kwaivgi-kling-video-o3-4k-image-to-video",
    "kwaivgi-kling-video-o3-4k-reference-to-video",
    "kwaivgi-kling-video-o3-4k-text-to-video",
    "kwaivgi-kling-video-o3-pro-image-to-video",
    "kwaivgi-kling-video-o3-pro-reference-to-video",
    "kwaivgi-kling-video-o3-pro-text-to-video",
    "kwaivgi-kling-video-o3-pro-video-edit",
    "kwaivgi-kling-video-o3-std-image-to-video",
    "kwaivgi-kling-video-o3-std-reference-to-video",
    "kwaivgi-kling-video-o3-std-text-to-video",
    "kwaivgi-kling-video-o3-std-video-edit",
    "kwaivgi-kling-video-to-audio",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/kwaivgi/{slug}"


PROVIDER_MODELS: list[GenerationModel] = [
    create_wavespeed_model_from_docs_url(_docs_url(slug), provider="kling")
    for slug in dict.fromkeys(KLING_MODEL_SLUGS)
]
