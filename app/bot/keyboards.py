"""Клавиатуры для бота."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


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


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура отмены активного сценария."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Отмена")],
            [KeyboardButton(text="⬅️ Назад в меню")],
        ],
        resize_keyboard=True,
    )


def get_generation_models_keyboard() -> InlineKeyboardMarkup:
    """Выбор модели генерации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Nano Banana Pro Edit Ultra", callback_data="generation:model:nano_banana")],
            [InlineKeyboardButton(text="Seedream V4.5 Edit", callback_data="generation:model:seedream")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
        ]
    )


def get_generation_confirm_keyboard() -> InlineKeyboardMarkup:
    """Кнопки подтверждения запуска генерации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустить", callback_data="generation:confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="generation:cancel")],
        ]
    )


def get_back_to_menu_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн кнопка возврата в меню."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")],
        ]
    )
