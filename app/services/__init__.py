"""Инициализация пакета services."""
from app.services.generation_service import (
    GENERATION_MODELS,
    GenerationModel,
    GenerationSetting,
    GenerationService,
    SettingOption,
    build_payload,
    get_default_settings,
    get_generation_model,
    list_generation_models,
    validate_model_settings,
)
from app.services.telegram_files import TelegramFilesService
from app.services.wavespeed import (
    WavespeedResult,
    WavespeedService,
    WavespeedSubmitResult,
    extract_error_message,
    extract_output_urls,
    extract_prediction_id,
    normalize_status,
)

__all__ = [
    "WavespeedService",
    "WavespeedSubmitResult",
    "WavespeedResult",
    "extract_prediction_id",
    "normalize_status",
    "extract_output_urls",
    "extract_error_message",
    "TelegramFilesService",
    "SettingOption",
    "GenerationSetting",
    "GenerationModel",
    "GenerationService",
    "GENERATION_MODELS",
    "get_generation_model",
    "list_generation_models",
    "get_default_settings",
    "validate_model_settings",
    "build_payload",
]
