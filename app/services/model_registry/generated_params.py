"""Generated Wavespeed model parameter metadata.

This file is maintained by scripts/sync_wavespeed_model_params.py.
Runtime code imports this static snapshot only; it must not perform network I/O.
"""
from __future__ import annotations


GENERATED_MODEL_PARAMS = {
    "bytedance_seedream_v3_1": {
        "allowed_payload_fields": ["prompt", "size", "enable_sync_mode", "enable_base64_output"],
        "base_wavespeed_price_usd": "0.03",
        "docs_url": "https://wavespeed.ai/docs/docs-api/bytedance/bytedance-seedream-v3.1",
        "last_synced_at": "manual-seed",
        "pricing_rules": {
            "output_count_fields": ["max_images", "num_images", "output_count"],
            "output_count_multiplier": True,
            "resolution_multipliers": {
                "512*512": 0.7,
                "768*768": 0.85,
                "1024*1024": 1.0,
                "1280*720": 1.1,
                "720*1280": 1.1,
                "1536*1536": 1.8,
                "2048*2048": 2.5,
                "4096*4096": 4.0,
            },
        },
        "required_fields": ["prompt"],
        "system_settings": {"enable_base64_output": False, "enable_sync_mode": False},
        "user_settings": {
            "size": {
                "default": "1024*1024",
                "options": ["512*512", "768*768", "1024*1024", "1280*720", "720*1280", "1536*1536", "2048*2048", "4096*4096"],
                "title": "Размер",
                "type": "enum",
            }
        },
    },
    "bytedance_seedream_v4_5_edit": {
        "allowed_payload_fields": ["images", "prompt", "size", "enable_sync_mode", "enable_base64_output"],
        "base_wavespeed_price_usd": "0.04",
        "docs_url": "https://wavespeed.ai/docs/docs-api/bytedance/bytedance-seedream-v4.5-edit",
        "last_synced_at": "manual-seed",
        "max_images": 10,
        "pricing_rules": {
            "output_count_fields": ["max_images", "num_images", "output_count"],
            "output_count_multiplier": True,
            "resolution_multipliers": {
                "512*512": 0.7,
                "768*768": 0.85,
                "1024*1024": 1.0,
                "1280*720": 1.1,
                "720*1280": 1.1,
                "1536*1536": 1.8,
                "2048*2048": 2.5,
                "4096*4096": 4.0,
            },
        },
        "required_fields": ["images", "prompt"],
        "supports_multiple_images": True,
        "system_settings": {"enable_base64_output": False, "enable_sync_mode": False},
        "user_settings": {
            "size": {
                "default": "1024*1024",
                "options": ["512*512", "768*768", "1024*1024", "1280*720", "720*1280", "1536*1536", "2048*2048", "4096*4096"],
                "title": "Размер",
                "type": "enum",
            }
        },
    },
    "bytedance_seedream_v4_sequential": {
        "allowed_payload_fields": ["prompt", "size", "enable_sync_mode", "enable_base64_output"],
        "base_wavespeed_price_usd": "0.03",
        "docs_url": "https://wavespeed.ai/docs/docs-api/bytedance/bytedance-seedream-v4-sequential",
        "last_synced_at": "manual-seed",
        "pricing_rules": {
            "output_count_fields": ["max_images", "num_images", "output_count"],
            "output_count_multiplier": True,
            "resolution_multipliers": {
                "512*512": 0.7,
                "768*768": 0.85,
                "1024*1024": 1.0,
                "1280*720": 1.1,
                "720*1280": 1.1,
                "1536*1536": 1.8,
                "2048*2048": 2.5,
                "4096*4096": 4.0,
            },
        },
        "required_fields": ["prompt"],
        "system_settings": {"enable_base64_output": False, "enable_sync_mode": False},
        "user_settings": {
            "size": {
                "default": "1024*1024",
                "options": ["512*512", "768*768", "1024*1024", "1280*720", "720*1280", "1536*1536", "2048*2048", "4096*4096"],
                "title": "Размер",
                "type": "enum",
            }
        },
    },
    "google_nano_banana_pro_edit_ultra": {
        "allowed_payload_fields": ["images", "prompt", "aspect_ratio", "resolution", "output_format", "enable_sync_mode", "enable_base64_output"],
        "base_wavespeed_price_usd": "0.14",
        "docs_url": "https://wavespeed.ai/docs/docs-api/google/google-nano-banana-pro-edit-ultra",
        "last_synced_at": "manual-seed",
        "max_images": 14,
        "pricing_rules": {
            "aspect_ratio_multipliers": {"1:1": 1.0, "3:2": 1.05, "2:3": 1.05, "3:4": 1.1, "4:3": 1.1, "4:5": 1.1, "5:4": 1.1, "16:9": 1.2, "9:16": 1.2, "21:9": 1.35},
            "resolution_multipliers": {"4k": 1.0, "8k": 2.0},
        },
        "required_fields": ["images", "prompt"],
        "supports_multiple_images": True,
        "system_settings": {"enable_base64_output": False, "enable_sync_mode": False},
        "user_settings": {
            "aspect_ratio": {"default": "1:1", "options": ["1:1", "3:2", "2:3", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"], "title": "Формат", "type": "enum"},
            "output_format": {"default": "png", "options": ["png", "jpeg"], "title": "Формат файла", "type": "enum"},
            "resolution": {"default": "4k", "options": ["4k", "8k"], "title": "Разрешение", "type": "enum"},
        },
    },
    "google_veo3": {
        "allowed_payload_fields": ["prompt", "duration", "resolution", "aspect_ratio", "enable_sync_mode", "enable_base64_output"],
        "base_wavespeed_price_usd": "0.22",
        "docs_url": "https://wavespeed.ai/docs/docs-api/google/google-veo3",
        "last_synced_at": "manual-seed",
        "pricing_rules": {
            "aspect_ratio_multipliers": {"16:9": 1.0, "9:16": 1.0},
            "duration_multiplier_per_second": True,
            "quality_multipliers": {"fast": 1.0, "standard": 1.5, "high": 2.2},
            "resolution_multipliers": {"720p": 1.0, "1080p": 1.8, "2k": 2.4, "4k": 3.5, "1280*720": 1.0, "720*1280": 1.0, "1920*1080": 1.8, "1080*1920": 1.8},
        },
        "required_fields": ["prompt"],
        "system_settings": {"enable_base64_output": False, "enable_sync_mode": False},
        "user_settings": {
            "aspect_ratio": {"default": "16:9", "options": ["16:9", "9:16"], "title": "Формат", "type": "enum"},
            "duration": {"default": "8", "max_value": "8", "min_value": "5", "options": ["5", "8"], "title": "Длительность", "type": "integer"},
            "resolution": {"default": "720p", "options": ["720p", "1080p"], "title": "Разрешение", "type": "enum"},
        },
    },
    "google_veo3_1_fast_video_extend": {
        "allowed_payload_fields": ["video", "prompt", "enable_sync_mode", "enable_base64_output"],
        "base_wavespeed_price_usd": "0.12",
        "docs_url": "https://wavespeed.ai/docs/docs-api/google/google-veo3.1-fast-video-extend",
        "last_synced_at": "manual-seed",
        "pricing_rules": {
            "aspect_ratio_multipliers": {"16:9": 1.0, "9:16": 1.0},
            "duration_multiplier_per_second": True,
            "quality_multipliers": {"fast": 1.0, "standard": 1.5, "high": 2.2},
            "resolution_multipliers": {"720p": 1.0, "1080p": 1.8, "2k": 2.4, "4k": 3.5, "1280*720": 1.0, "720*1280": 1.0, "1920*1080": 1.8, "1080*1920": 1.8},
        },
        "required_fields": ["video", "prompt"],
        "requires_prompt": True,
        "system_settings": {"enable_base64_output": False, "enable_sync_mode": False},
        "user_settings": {},
    },
    "google_veo3_fast": {
        "allowed_payload_fields": ["prompt", "duration", "resolution", "aspect_ratio", "enable_sync_mode", "enable_base64_output"],
        "base_wavespeed_price_usd": "0.12",
        "docs_url": "https://wavespeed.ai/docs/docs-api/google/google-veo3-fast",
        "last_synced_at": "manual-seed",
        "pricing_rules": {
            "aspect_ratio_multipliers": {"16:9": 1.0, "9:16": 1.0},
            "duration_multiplier_per_second": True,
            "quality_multipliers": {"fast": 1.0, "standard": 1.5, "high": 2.2},
            "resolution_multipliers": {"720p": 1.0, "1080p": 1.8, "2k": 2.4, "4k": 3.5, "1280*720": 1.0, "720*1280": 1.0, "1920*1080": 1.8, "1080*1920": 1.8},
        },
        "required_fields": ["prompt"],
        "system_settings": {"enable_base64_output": False, "enable_sync_mode": False},
        "user_settings": {
            "aspect_ratio": {"default": "16:9", "options": ["16:9", "9:16"], "title": "Формат", "type": "enum"},
            "duration": {"default": "8", "max_value": "8", "min_value": "5", "options": ["5", "8"], "title": "Длительность", "type": "integer"},
            "resolution": {"default": "720p", "options": ["720p", "1080p"], "title": "Разрешение", "type": "enum"},
        },
    },
}
