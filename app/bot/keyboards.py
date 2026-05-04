"""Клавиатуры для бота."""
from __future__ import annotations

from typing import Any, Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


CALLBACK_DATA_LIMIT = 64


def _ensure_callback_data(callback_data: str) -> str:
    """Проверить, что callback_data укладывается в лимит Telegram."""
    if len(callback_data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
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
    """Получить упорядоченный список настроек модели для index-based callback_data."""
    return list(model.user_settings.items())


def get_setting_key_by_index(model: Any, setting_index: int) -> str | None:
    """Восстановить ключ настройки по индексу."""
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


def build_model_selection_keyboard(models: Iterable[Any]) -> InlineKeyboardMarkup:
    """Построить клавиатуру выбора модели из реестра."""
    rows = [
        [
            InlineKeyboardButton(
                text=str(model.title),
                callback_data=_ensure_callback_data(f"gen:model:{model.key}"),
            )
        ]
        for model in models
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_model_settings_keyboard(model: Any, current_settings: dict[str, Any]) -> InlineKeyboardMarkup:
    """Построить клавиатуру настроек модели."""
    rows = []
    for setting_index, (_setting_key, setting) in enumerate(get_model_setting_entries(model)):
        current_value = str(current_settings.get(setting.key, setting.default))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{getattr(setting, 'title', setting.key)}: {current_value}",
                    callback_data=_ensure_callback_data(f"gen:setting:{setting_index}"),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✅ Продолжить", callback_data="gen:continue")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к моделям", callback_data="gen:back_models")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_setting_options_keyboard(
    setting_index: int,
    options: Iterable[Any],
    current_value: str,
) -> InlineKeyboardMarkup:
    """Построить клавиатуру вариантов значения настройки."""
    rows = []
    for option_index, (value, label) in enumerate(_get_setting_options(options)):
        marker = "✅ " if value == current_value else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}{label}",
                    callback_data=_ensure_callback_data(f"gen:set:{setting_index}:{option_index}"),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад к настройкам", callback_data="gen:back_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_generation_confirm_keyboard() -> InlineKeyboardMarkup:
    """Кнопки подтверждения запуска генерации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить", callback_data="gen:confirm")],
            [InlineKeyboardButton(text="⚙️ Изменить настройки", callback_data="gen:edit")],
        ]
    )


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
