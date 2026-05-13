"""Generation models for the wavespeed_ai provider."""
from __future__ import annotations

from decimal import Decimal

from .base import GenerationModel, GenerationSetting, SettingOption, create_wavespeed_model_from_docs_url


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
] + [
    GenerationModel(
        key="wan_ai_image_upscaler",
        title="Image Upscaler",
        provider="wavespeed_ai",
        generation_type="image_to_image",
        endpoint="https://api.wavespeed.ai/api/v3/wavespeed-ai/image-upscaler",
        docs_url="https://wavespeed.ai/docs/docs-api/wavespeed-ai/image-upscaler",
        description="Upscale an image to a higher resolution.",
        max_images=1,
        min_images=1,
        requires_prompt=False,
        requires_image=True,
        requires_video=False,
        requires_audio=False,
        outputs="image",
        input_media_field="image",
        required_payload_fields=("image",),
        allowed_payload_fields=(
            "image",
            "target_resolution",
            "output_format",
            "enable_base64_output",
            "enable_sync_mode",
        ),
        payload_mapping={"images": "image"},
        input_requirements={
            "prompt": {"required": False, "payload_field": "prompt"},
            "images": {"required": True, "min": 1, "max": 1, "payload_field": "image"},
            "video": {"required": False},
            "audio": {"required": False},
        },
        user_settings={
            "target_resolution": GenerationSetting(
                key="target_resolution",
                title="Target resolution",
                type="select",
                default="4k",
                options=(
                    SettingOption(value="2k", label="2k"),
                    SettingOption(value="4k", label="4k"),
                    SettingOption(value="8k", label="8k"),
                ),
            ),
            "output_format": GenerationSetting(
                key="output_format",
                title="Output format",
                type="select",
                default="jpeg",
                options=(
                    SettingOption(value="jpeg", label="jpeg"),
                    SettingOption(value="png", label="png"),
                    SettingOption(value="webp", label="webp"),
                ),
            ),
            "num_generations": GenerationSetting(
                key="num_generations",
                title="Generation count",
                type="select",
                default="1",
                options=tuple(SettingOption(value=str(value), label=str(value)) for value in range(1, 11)),
            ),
        },
        system_settings={
            "enable_base64_output": False,
            "enable_sync_mode": False,
        },
        base_wavespeed_price_usd=Decimal("0.01"),
    )
]
