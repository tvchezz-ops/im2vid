"""Клавиатуры для бота."""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.services.generation_service import list_generation_types, list_providers


CALLBACK_DATA_LIMIT = 64

SECTION_LABELS = {
    "text_to_image": "🖼 Text → Image",
    "text_to_video": "🎬 Text → Video",
    "image_edit": "🛠 Image Edit",
    "image_to_video": "🎥 Image → Video",
    "video_edit": "🎞 Video Edit",
    "lipsync": "🗣 Lipsync",
}

PROVIDER_LABELS = {
    "alibaba": "Alibaba",
    "openai": "OpenAI",
    "bytedance": "ByteDance",
    "google": "Google",
}


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


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎨 Генерации")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🛒 Магазин")],
        ],
        resize_keyboard=True,
    )


def get_back_to_menu_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура возврата в меню."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад в меню")]],
        resize_keyboard=True,
    )


def build_back_to_settings_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard для возврата с этапа ввода к настройкам модели."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад к настройкам")]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_media_upload_reply_keyboard(*, show_continue: bool) -> ReplyKeyboardMarkup:
    """Reply keyboard для этапа загрузки media с optional continue action."""
    keyboard = [[KeyboardButton(text="⬅️ Назад к настройкам")]]
    if show_continue:
        keyboard.insert(0, [KeyboardButton(text="✅ Продолжить")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_generation_sections_keyboard() -> InlineKeyboardMarkup:
    """Построить клавиатуру разделов генерации по enabled registry."""
    rows = [
        [
            InlineKeyboardButton(
                text=SECTION_LABELS[generation_type],
                callback_data=validate_callback_length(f"gen:section:{generation_type}"),
            )
        ]
        for generation_type in list_generation_types()
        if generation_type in SECTION_LABELS
    ]
    rows.append([InlineKeyboardButton(text="📚 All List", callback_data="gen:all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_providers_keyboard() -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton(text="⬅️ Назад к разделам", callback_data="gen:back:sections")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_models_keyboard(models: Iterable[Any], back_callback: str) -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=validate_callback_length(back_callback))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_model_settings_keyboard(model: Any, current_settings: dict[str, Any]) -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton(text="✅ Продолжить", callback_data="gen:continue")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к моделям", callback_data="gen:back:models")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_setting_options_keyboard(model: Any, setting_key: str, current_value: str) -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton(text="⬅️ Назад к настройкам", callback_data="gen:back:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_generation_confirm_keyboard() -> InlineKeyboardMarkup:
    """Кнопки подтверждения запуска генерации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить", callback_data="gen:confirm")],
            [InlineKeyboardButton(text="⬅️ Назад к настройкам", callback_data="gen:back:settings")],
        ]
    )


def build_back_to_settings_keyboard() -> ReplyKeyboardMarkup:
    """Совместимость со старым именем reply keyboard возврата к настройкам."""
    return build_back_to_settings_reply_keyboard()


def build_generation_type_keyboard(options: Iterable[tuple[str, str]] | None = None) -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора разделов."""
    if options is None:
        return build_generation_sections_keyboard()

    rows = []
    for generation_type, label in options:
        callback_data = "gen:all" if generation_type == "all" else f"gen:section:{generation_type}"
        rows.append([InlineKeyboardButton(text=label, callback_data=validate_callback_length(callback_data))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_provider_keyboard(providers: Iterable[tuple[str, str]] | None = None) -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора провайдера."""
    if providers is None:
        return build_providers_keyboard()

    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=validate_callback_length(f"gen:provider:{provider}"),
            )
        ]
        for provider, label in providers
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад к разделам", callback_data="gen:back:sections")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_model_selection_keyboard(models: Iterable[Any]) -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции выбора модели."""
    return build_models_keyboard(models, "gen:back:sections")


def build_generation_type_selection_keyboard(options: Iterable[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора типа генерации."""
    return build_generation_type_keyboard(options)


def build_provider_selection_keyboard(providers: Iterable[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора провайдера."""
    return build_provider_keyboard(providers)


def get_generation_models_keyboard(models: Iterable[Any]) -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции выбора модели."""
    return build_model_selection_keyboard(models)


def get_generation_confirm_keyboard() -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции подтверждения."""
    return build_generation_confirm_keyboard()


def get_back_to_menu_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн кнопка возврата в меню."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")],
        ]
    )


def get_profile_keyboard(*, send_results_as_files: bool) -> InlineKeyboardMarkup:
    """Инлайн-кнопки профиля пользователя."""
    toggle_text = "🖼 Отправлять обычным форматом" if send_results_as_files else "📎 Отправлять файлом"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="profile:toggle_delivery_mode")],
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")],
        ]
    )
