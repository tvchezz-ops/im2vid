"""Клавиатуры для бота."""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.i18n import get_user_language, t
from app.services.generation_service import list_generation_types, list_providers


CALLBACK_DATA_LIMIT = 64

SECTION_KEYS = {
    "text_to_image": "generation.text_to_image",
    "text_to_video": "generation.text_to_video",
    "image_edit": "generation.image_edit",
    "image_to_video": "generation.image_to_video",
    "video_edit": "generation.video_edit",
    "lipsync": "generation.lipsync",
}

BUTTON_ICONS = {
    "main.generations": "🎨",
    "main.profile": "👤",
    "main.shop": "🛒",
    "common.back": "⬅️",
    "common.back_to_settings": "⬅️",
    "common.continue": "✅",
    "common.download_file": "🔗",
    "common.change_settings": "⚙️",
    "profile.top_up": "💳",
    "profile.toggle_delivery": "📎",
    "profile.history": "📜",
    "generation.text_to_image": "🖼",
    "generation.text_to_video": "🎬",
    "generation.image_edit": "🛠",
    "generation.image_to_video": "🎥",
    "generation.video_edit": "🎞",
    "generation.lipsync": "🗣",
    "generation.all_models": "📚",
    "generation.confirm": "🚀",
    "download.button": "🔗",
}

PROVIDER_LABELS = {
    "alibaba": "Alibaba",
    "openai": "OpenAI",
    "bytedance": "ByteDance",
    "google": "Google",
}


def get_button_text(key: str, lang: str = "en") -> str:
    """Build a localized button label with a stable icon prefix when configured."""
    resolved_lang = get_user_language(lang)
    icon = BUTTON_ICONS.get(key)
    label = t(key, resolved_lang)
    if icon:
        return f"{icon} {label}"
    return label


def is_localized_button_text(text: str | None, key: str, language_code: str | None) -> bool:
    """Check whether incoming text matches the localized keyboard label."""
    if text is None:
        return False
    return text == get_button_text(key, get_user_language(language_code))


def validate_callback_length(callback_data: str) -> str:
    """Проверить, что callback_data укладывается в лимит Telegram."""
    if len(callback_data.encode("utf-8")) >= CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback_data is too long: {callback_data}")
    return callback_data


def _get_setting_options(options: Iterable[Any]) -> list[tuple[str, str]]:
    """Нормализовать опции настройки в пары value/label."""
    normalized_options: list[tuple[str, str]] = []
    for option in options:
        value = str(getattr(option, "value", option))
        label = str(getattr(option, "label", value))
        normalized_options.append((value, label))
    return normalized_options


def get_model_setting_entries(model: Any) -> list[tuple[str, Any]]:
    """Получить упорядоченный список настроек модели."""
    return list(model.user_settings.items())


def get_setting_key_by_index(model: Any, setting_index: int) -> str | None:
    """Совместимость: восстановить ключ настройки по индексу."""
    setting_entries = get_model_setting_entries(model)
    if setting_index < 0 or setting_index >= len(setting_entries):
        return None
    return setting_entries[setting_index][0]


def get_option_value_by_index(model: Any, setting_key: str, option_index: int) -> str | None:
    """Восстановить значение опции настройки по индексу."""
    setting = model.user_settings.get(setting_key)
    if setting is None:
        return None
    options = _get_setting_options(setting.options)
    if option_index < 0 or option_index >= len(options):
        return None
    return options[option_index][0]


def get_model_callback_token(models: Sequence[Any], model: Any, model_index: int) -> str:
    """Получить token для callback выбора модели, укладывающийся в лимит Telegram."""
    candidate_tokens = [str(getattr(model, "key", ""))]
    short_key = getattr(model, "short_key", None)
    if short_key:
        candidate_tokens.append(str(short_key))
    candidate_tokens.append(f"i{model_index}")

    for token in candidate_tokens:
        if not token:
            continue
        try:
            validate_callback_length(f"gen:model:{token}")
        except ValueError:
            continue
        return token

    raise ValueError(f"callback_data is too long for model: {getattr(model, 'key', model_index)}")


def resolve_model_key_from_token(models: Sequence[Any], token: str) -> str | None:
    """Восстановить оригинальный model_key из callback token."""
    if token.startswith("i") and token[1:].isdigit():
        model_index = int(token[1:])
        if 0 <= model_index < len(models):
            return str(models[model_index].key)
        return None

    for model in models:
        if token == str(getattr(model, "key", "")):
            return str(model.key)
        short_key = getattr(model, "short_key", None)
        if short_key is not None and token == str(short_key):
            return str(model.key)
    return None


def build_main_menu_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """Главное меню Telegram reply keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=get_button_text("main.generations", lang))],
            [
                KeyboardButton(text=get_button_text("main.profile", lang)),
                KeyboardButton(text=get_button_text("main.shop", lang)),
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder=t("main.placeholder", lang),
    )


def get_main_menu_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """Совместимость со старым именем главного меню."""
    return build_main_menu_keyboard(lang)


def get_back_to_menu_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """Клавиатура возврата в меню."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=get_button_text("common.back", lang))]],
        resize_keyboard=True,
    )


def build_back_to_settings_reply_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """Reply keyboard для возврата с этапа ввода к настройкам модели."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=get_button_text("common.back_to_settings", lang))]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_media_upload_reply_keyboard(*, show_continue: bool, lang: str = "en") -> ReplyKeyboardMarkup:
    """Reply keyboard для этапа загрузки media с optional continue action."""
    keyboard = [[KeyboardButton(text=get_button_text("common.back_to_settings", lang))]]
    if show_continue:
        keyboard.insert(0, [KeyboardButton(text=get_button_text("common.continue", lang))])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_generation_sections_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    """Построить клавиатуру разделов генерации по enabled registry."""
    rows = [
        [
            InlineKeyboardButton(
                text=get_button_text(SECTION_KEYS[generation_type], lang),
                callback_data=validate_callback_length(f"gen:section:{generation_type}"),
            )
        ]
        for generation_type in list_generation_types()
        if generation_type in SECTION_KEYS
    ]
    rows.append([InlineKeyboardButton(text=get_button_text("generation.all_models", lang), callback_data="gen:all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_providers_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    """Построить клавиатуру провайдеров из registry."""
    rows = [
        [
            InlineKeyboardButton(
                text=PROVIDER_LABELS.get(provider, provider.title()),
                callback_data=validate_callback_length(f"gen:provider:{provider}"),
            )
        ]
        for provider in list_providers()
    ]
    rows.append([InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="gen:back:sections")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_models_keyboard(models: Iterable[Any], back_callback: str, lang: str = "en") -> InlineKeyboardMarkup:
    """Построить клавиатуру моделей с настраиваемым callback возврата."""
    model_list = list(models)
    rows = []
    for model_index, model in enumerate(model_list):
        token = get_model_callback_token(model_list, model, model_index)
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(model.title),
                    callback_data=validate_callback_length(f"gen:model:{token}"),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data=validate_callback_length(back_callback))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_model_settings_keyboard(model: Any, current_settings: dict[str, Any], lang: str = "en") -> InlineKeyboardMarkup:
    """Построить клавиатуру настроек модели."""
    rows = []
    for setting_key, setting in get_model_setting_entries(model):
        current_value = str(current_settings.get(setting.key, setting.default))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{getattr(setting, 'title', setting.key)}: {current_value}",
                    callback_data=validate_callback_length(f"gen:setting:{setting_key}"),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=get_button_text("common.continue", lang), callback_data="gen:continue")])
    rows.append([InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="gen:back:models")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_setting_options_keyboard(model: Any, setting_key: str, current_value: str, lang: str = "en") -> InlineKeyboardMarkup:
    """Построить клавиатуру вариантов значения настройки."""
    setting = model.user_settings[setting_key]
    rows = []
    for option_index, (value, label) in enumerate(_get_setting_options(setting.options)):
        marker = "✅ " if value == current_value else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=validate_callback_length(f"gen:set:{setting_key}:{option_index}"),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=get_button_text("common.back_to_settings", lang), callback_data="gen:back:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_generation_confirm_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    """Кнопки подтверждения запуска генерации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=get_button_text("generation.confirm", lang), callback_data="gen:confirm")],
            [InlineKeyboardButton(text=get_button_text("common.change_settings", lang), callback_data="gen:back:settings")],
        ]
    )


def build_back_to_settings_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """Совместимость со старым именем reply keyboard возврата к настройкам."""
    return build_back_to_settings_reply_keyboard(lang)


def build_generation_type_keyboard(options: Iterable[tuple[str, str]] | None = None, lang: str = "en") -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора разделов."""
    if options is None:
        return build_generation_sections_keyboard(lang)

    rows = []
    for generation_type, label in options:
        callback_data = "gen:all" if generation_type == "all" else f"gen:section:{generation_type}"
        rows.append([InlineKeyboardButton(text=label, callback_data=validate_callback_length(callback_data))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_provider_keyboard(providers: Iterable[tuple[str, str]] | None = None, lang: str = "en") -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора провайдера."""
    if providers is None:
        return build_providers_keyboard(lang)

    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=validate_callback_length(f"gen:provider:{provider}"),
            )
        ]
        for provider, label in providers
    ]
    rows.append([InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="gen:back:sections")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_model_selection_keyboard(models: Iterable[Any], lang: str = "en") -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции выбора модели."""
    return build_models_keyboard(models, "gen:back:sections", lang)


def build_generation_type_selection_keyboard(options: Iterable[tuple[str, str]], lang: str = "en") -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора типа генерации."""
    return build_generation_type_keyboard(options, lang)


def build_provider_selection_keyboard(providers: Iterable[tuple[str, str]], lang: str = "en") -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора провайдера."""
    return build_provider_keyboard(providers, lang)


def get_generation_models_keyboard(models: Iterable[Any], lang: str = "en") -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции выбора модели."""
    return build_model_selection_keyboard(models, lang)


def get_generation_confirm_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции подтверждения."""
    return build_generation_confirm_keyboard(lang)


def get_back_to_menu_inline_keyboard(lang: str = "en") -> InlineKeyboardMarkup:
    """Инлайн кнопка возврата в меню."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="back_to_menu")],
        ]
    )


def get_profile_keyboard(*, send_results_as_files: bool, lang: str = "en") -> InlineKeyboardMarkup:
    """Инлайн-кнопки профиля пользователя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=get_button_text("profile.top_up", lang), callback_data="profile:top_up_balance")],
            [InlineKeyboardButton(text=get_button_text("profile.toggle_delivery", lang), callback_data="profile:toggle_delivery_mode")],
            [InlineKeyboardButton(text=get_button_text("profile.history", lang), callback_data="profile:generation_history")],
            [InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="back_to_menu")],
        ]
    )
