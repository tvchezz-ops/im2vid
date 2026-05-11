"""Generation models for the minimax provider."""
from __future__ import annotations

from .base import GenerationModel, create_wavespeed_model_from_docs_url


MINIMAX_MODEL_SLUGS = (
    "minimax-hailuo-02-fast",
    "minimax-hailuo-02-i2v-pro",
    "minimax-hailuo-02-i2v-standard",
    "minimax-hailuo-02-pro",
    "minimax-hailuo-02-standard",
    "minimax-hailuo-02-t2v-pro",
    "minimax-hailuo-02-t2v-standard",
    "minimax-hailuo-2.3-fast",
    "minimax-hailuo-2.3-fast-pro",
    "minimax-hailuo-2.3-i2v-pro",
    "minimax-hailuo-2.3-i2v-standard",
    "minimax-hailuo-2.3-t2v-pro",
    "minimax-hailuo-2.3-t2v-standard",
    "minimax-image-01-image-to-image",
    "minimax-image-01-text-to-image",
    "minimax-video-01",
    "minimax-video-02",
)


def _docs_url(slug: str) -> str:
    return f"https://wavespeed.ai/docs/docs-api/minimax/{slug}"


PROVIDER_MODELS: list[GenerationModel] = [
    create_wavespeed_model_from_docs_url(_docs_url(slug), provider="minimax")
    for slug in dict.fromkeys(MINIMAX_MODEL_SLUGS)
]
