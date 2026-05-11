"""Generation models for the wavespeed_ai provider."""
from __future__ import annotations

from .base import GenerationModel, create_wavespeed_model_from_docs_url


WAVESPEED_AI_MODEL_SLUGS = (
    "wan-2.2-animate",
    "wan-2.2-fun-control",
    "wan-2.2-i2v-480p",
    "wan-2.2-i2v-480p-lora",
    "wan-2.2-i2v-480p-lora-ultra-fast",
    "wan-2.2-i2v-480p-ultra-fast",
    "wan-2.2-i2v-5b-720p",
    "wan-2.2-i2v-5b-720p-lora",
    "wan-2.2-i2v-720p",
    "wan-2.2-i2v-720p-lora",
    "wan-2.2-i2v-720p-lora-ultra-fast",
    "wan-2.2-i2v-720p-ultra-fast",
    "wan-2.2-image-to-image",
    "wan-2.2-image-to-video",
    "wan-2.2-image-to-video-lora",
    "wan-2.2-speech-to-video",
    "wan-2.2-spicy-image-to-video",
    "wan-2.2-spicy-image-to-video-lora",
    "wan-2.2-spicy-video-extend",
    "wan-2.2-spicy-video-extend-lora",
    "wan-2.2-t2v-480p",
    "wan-2.2-t2v-480p-lora",
    "wan-2.2-t2v-480p-lora-ultra-fast",
    "wan-2.2-t2v-480p-ultra-fast",
    "wan-2.2-t2v-5b-720p",
    "wan-2.2-t2v-5b-720p-lora",
    "wan-2.2-t2v-720p",
    "wan-2.2-t2v-720p-lora",
    "wan-2.2-t2v-720p-lora-ultra-fast",
    "wan-2.2-t2v-720p-ultra-fast",
    "wan-2.2-text-to-image-lora",
    "wan-2.2-text-to-image-realism",
    "wan-2.2-video-edit",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/wavespeed-ai/{slug}"


PROVIDER_MODELS: list[GenerationModel] = [
    create_wavespeed_model_from_docs_url(_docs_url(slug), provider="wavespeed_ai")
    for slug in dict.fromkeys(WAVESPEED_AI_MODEL_SLUGS)
]
