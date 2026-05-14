"""Клавиатуры для бота."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.error_messages import build_error_keyboard
from app.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, get_user_language, t
from app.services.payments import ALLOWED_STARS_AMOUNTS
from app.services.generation_service import (
    generation_cost_has_minimum_label,
    get_minimum_generation_cost_credits,
    is_generation_cost_estimated,
    list_generation_types,
    list_providers,
)


CALLBACK_DATA_LIMIT = 64
DEFAULT_PAGE_SIZE = 8
PAGINATION_NOOP_CALLBACK = "gen:page:noop"
NEGATIVE_PROMPT_SETTING_KEYS = {"exclude", "excluded_prompt", "negative", "negative_prompt", "avoid_prompt"}
NEGATIVE_PROMPT_LEGACY_TITLES = {"exclude", "excluded prompt", "negative", "negative prompt", "avoid prompt", "исключить"}

SECTION_KEYS = {
    "text_to_image": "generation.text_to_image",
    "image_to_image": "generation.image_to_image",
    "image_edit": "generation.image_edit",
    "text_to_video": "generation.text_to_video",
    "image_to_video": "generation.image_to_video",
    "reference_to_video": "generation.reference_to_video",
    "video_edit": "generation.video_edit",
    "video_extend": "generation.video_extend",
    "lipsync": "generation.lipsync",
    "motion_control": "generation.motion_control",
    "avatar": "generation.avatar",
    "audio_to_video": "generation.audio_to_video",
    "video_to_audio": "generation.video_to_audio",
    "effects": "generation.effects",
}

BUTTON_ICONS = {
    "main.generations": "🎨",
    "main.profile": "👤",
    "common.back": "⬅️",
    "common.back_to_settings": "⬅️",
    "common.continue": "✅",
    "common.clear_images": "🗑",
    "common.download_file": "🔗",
    "common.change_settings": "⚙️",
    "profile.top_up": "💳",
    "profile.toggle_delivery": "⚙️",
    "payments.back_to_profile": "⬅️",
    "payments.pay_here": "⭐",
    "generation.text_to_image": "🖼",
    "generation.image_to_image": "🖼",
    "generation.image_edit": "🛠",
    "generation.text_to_video": "🎬",
    "generation.image_to_video": "🎥",
    "generation.reference_to_video": "🧭",
    "generation.video_edit": "🎞",
    "generation.video_extend": "⏩",
    "generation.lipsync": "🗣",
    "generation.motion_control": "🎚",
    "generation.avatar": "👥",
    "generation.audio_to_video": "🎙",
    "generation.video_to_audio": "🔊",
    "generation.effects": "✨",
    "generation.all_models": "📚",
    "generation.confirm": "🚀",
    "generation.summary.repeat": "🔁",
    "download.button": "🔗",
}

PROVIDER_LABELS = {
    "alibaba": "Alibaba",
    "bytedance": "ByteDance",
    "google": "Google",
    "openai": "OpenAI",
    "kling": "Kling",
    "grok": "Grok",
    "minimax": "MiniMax",
    "wavespeed_ai": "Wan AI",
}


def get_button_text(key: str, lang: str = DEFAULT_LANGUAGE) -> str:
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
    candidate_languages = (get_user_language(language_code), *SUPPORTED_LANGUAGES)
    return any(text == get_button_text(key, language) for language in dict.fromkeys(candidate_languages))


def validate_callback_length(callback_data: str) -> str:
    """Проверить, что callback_data укладывается в лимит Telegram."""
    if len(callback_data.encode("utf-8")) >= CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback_data is too long: {callback_data}")
    return callback_data


def _call_item_callback_builder(
    item_callback_builder: Callable[..., str],
    item: Any,
    item_index: int,
) -> str:
    try:
        return item_callback_builder(item, item_index)
    except TypeError:
        return item_callback_builder(item)


def _default_item_text(item: Any) -> str:
    title = getattr(item, "title", None)
    if title is not None and not callable(title):
        return str(title)
    return str(item)


def build_paginated_keyboard(
    items: Iterable[Any],
    page: int,
    page_size: int = DEFAULT_PAGE_SIZE,
    *,
    item_callback_builder: Callable[..., str],
    back_callback: str,
    item_text_builder: Callable[[Any], str] | None = None,
    page_callback_builder: Callable[[int], str] | None = None,
    columns: int = 1,
    hide_unavailable_navigation: bool = True,
    lang: str = DEFAULT_LANGUAGE,
) -> InlineKeyboardMarkup:
    """Build a paginated inline keyboard with navigation and a back row."""
    item_list = list(items)
    safe_page_size = max(1, page_size)
    safe_columns = max(1, columns)
    total_pages = max(1, (len(item_list) + safe_page_size - 1) // safe_page_size)
    current_page = min(max(page, 0), total_pages - 1)
    start_index = current_page * safe_page_size
    page_items = item_list[start_index:start_index + safe_page_size]

    rows: list[list[InlineKeyboardButton]] = []
    item_row: list[InlineKeyboardButton] = []
    for offset, item in enumerate(page_items):
        item_index = start_index + offset
        text = item_text_builder(item) if item_text_builder is not None else _default_item_text(item)
        item_row.append(
            InlineKeyboardButton(
                text=text,
                callback_data=validate_callback_length(_call_item_callback_builder(item_callback_builder, item, item_index)),
            )
        )
        if len(item_row) == safe_columns:
            rows.append(item_row)
            item_row = []
    if item_row:
        rows.append(item_row)

    def page_callback(target_page: int) -> str:
        if page_callback_builder is None:
            return PAGINATION_NOOP_CALLBACK
        return page_callback_builder(target_page)

    previous_page = max(current_page - 1, 0)
    next_page = min(current_page + 1, total_pages - 1)
    if total_pages > 1:
        navigation_row: list[InlineKeyboardButton] = []
        if current_page > 0 or not hide_unavailable_navigation:
            navigation_row.append(
                InlineKeyboardButton(
                    text=t("pagination.prev", lang),
                    callback_data=validate_callback_length(page_callback(previous_page)),
                )
            )
        navigation_row.append(
            InlineKeyboardButton(
                text=t("pagination.page", lang, current=current_page + 1, total=total_pages),
                callback_data=validate_callback_length(PAGINATION_NOOP_CALLBACK),
            )
        )
        if current_page < total_pages - 1 or not hide_unavailable_navigation:
            navigation_row.append(
                InlineKeyboardButton(
                    text=t("pagination.next", lang),
                    callback_data=validate_callback_length(page_callback(next_page)),
                )
            )
        rows.append(navigation_row)
    rows.append([InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data=validate_callback_length(back_callback))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_setting_options(options: Iterable[Any]) -> list[tuple[str, str]]:
    """Нормализовать опции настройки в пары value/label."""
    normalized_options: list[tuple[str, str]] = []
    for option in options:
        value = str(getattr(option, "value", option))
        label = str(getattr(option, "label", value))
        normalized_options.append((value, label))
    return normalized_options


def get_setting_display_title(setting_key: str, setting: Any, lang: str = DEFAULT_LANGUAGE) -> str:
    if setting_key in NEGATIVE_PROMPT_SETTING_KEYS:
        return t("settings.title.negative_prompt", lang)
    direct_key = f"settings.{setting_key}"
    direct_title = t(direct_key, lang)
    if direct_title != direct_key:
        return direct_title
    title_key = f"settings.title.{setting_key}"
    translated_title = t(title_key, lang)
    if translated_title != title_key:
        return translated_title
    fallback_title = str(getattr(setting, "title", setting_key))
    if fallback_title.strip().casefold() in NEGATIVE_PROMPT_LEGACY_TITLES:
        return t("settings.title.negative_prompt", lang)
    return fallback_title


def get_setting_option_display_label(value: str, label: str, lang: str = DEFAULT_LANGUAGE) -> str:
    option_key = f"settings.option.{value}"
    translated_label = t(option_key, lang)
    if translated_label != option_key:
        return translated_label
    return label


def get_model_setting_entries(model: Any) -> list[tuple[str, Any]]:
    """Получить упорядоченный список настроек модели."""
    return [
        (setting_key, setting)
        for setting_key, setting in model.user_settings.items()
        if getattr(setting, "is_user_visible", True)
    ]


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


def format_model_price_label(model: Any, lang: str = DEFAULT_LANGUAGE) -> str:
    """Build a compact price suffix for model selection buttons."""
    try:
        cost = get_minimum_generation_cost_credits(model)
        prefix = "от " if get_user_language(lang) == "ru" and generation_cost_has_minimum_label(model) else ""
        if get_user_language(lang) != "ru" and generation_cost_has_minimum_label(model):
            prefix = "from "
        if is_generation_cost_estimated(model):
            prefix = f"{prefix}≈ "
        return f"{prefix}{cost} credits"
    except Exception:
        return ""


def build_main_menu_keyboard(lang: str = DEFAULT_LANGUAGE) -> ReplyKeyboardMarkup:
    """Главное меню Telegram reply keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=get_button_text("main.generations", lang)),
                KeyboardButton(text=get_button_text("main.profile", lang)),
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder=t("main.placeholder", lang),
    )


def get_main_menu_keyboard(lang: str = DEFAULT_LANGUAGE) -> ReplyKeyboardMarkup:
    """Совместимость со старым именем главного меню."""
    return build_main_menu_keyboard(lang)


def get_back_to_menu_keyboard(lang: str = DEFAULT_LANGUAGE) -> ReplyKeyboardMarkup:
    """Клавиатура возврата в меню."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=get_button_text("common.back", lang))]],
        resize_keyboard=True,
    )


def build_generation_summary_keyboard(batch_id: object, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Inline actions shown under the final generation summary."""
    callback_data = validate_callback_length(f"gen:repeat:{batch_id}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=get_button_text("generation.summary.repeat", lang), callback_data=callback_data)],
        ]
    )


def build_back_to_settings_reply_keyboard(lang: str = DEFAULT_LANGUAGE) -> ReplyKeyboardMarkup:
    """Reply keyboard для возврата с этапа ввода к настройкам модели."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=get_button_text("common.back_to_settings", lang))]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_media_upload_reply_keyboard(
    *,
    show_continue: bool,
    lang: str = DEFAULT_LANGUAGE,
    show_clear_images: bool = False,
) -> ReplyKeyboardMarkup:
    """Reply keyboard для этапа загрузки media с optional continue action."""
    keyboard = [[KeyboardButton(text=get_button_text("common.back_to_settings", lang))]]
    if show_clear_images:
        keyboard.insert(0, [KeyboardButton(text=get_button_text("common.clear_images", lang))])
    if show_continue:
        keyboard.insert(0, [KeyboardButton(text=get_button_text("common.continue", lang))])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_generation_sections_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
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


def build_providers_keyboard(lang: str = DEFAULT_LANGUAGE, page: int = 0) -> InlineKeyboardMarkup:
    """Построить клавиатуру провайдеров из registry."""
    return build_paginated_keyboard(
        list_providers(),
        page,
        item_callback_builder=lambda provider: f"gen:provider:{provider}:0",
        item_text_builder=lambda provider: PROVIDER_LABELS.get(provider, str(provider).title()),
        page_callback_builder=lambda target_page: f"gen:providers:{target_page}",
        back_callback="gen:back:sections",
        columns=2,
        lang=lang,
    )


def build_models_keyboard(
    models: Iterable[Any],
    back_callback: str,
    lang: str = DEFAULT_LANGUAGE,
    page: int = 0,
    page_callback_builder: Callable[[int], str] | None = None,
    show_price: bool = False,
) -> InlineKeyboardMarkup:
    """Построить клавиатуру моделей с настраиваемым callback возврата."""
    model_list = list(models)
    def build_model_callback(model: Any, model_index: int) -> str:
        token = get_model_callback_token(model_list, model, model_index)
        return f"gen:model:{token}"

    def build_model_text(model: Any) -> str:
        button_text = str(model.title)
        price_label = format_model_price_label(model, lang) if show_price else ""
        if show_price and price_label:
            button_text = f"{button_text} — {price_label}"
        return button_text

    return build_paginated_keyboard(
        model_list,
        page,
        item_callback_builder=build_model_callback,
        item_text_builder=build_model_text,
        page_callback_builder=page_callback_builder,
        back_callback=back_callback,
        columns=1,
        lang=lang,
    )


def build_model_settings_keyboard(model: Any, current_settings: dict[str, Any], lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Построить клавиатуру настроек модели."""
    rows = []
    for setting_key, setting in get_model_setting_entries(model):
        current_value = str(current_settings.get(setting.key, setting.default))
        setting_title = get_setting_display_title(setting_key, setting, lang)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{setting_title}: {current_value}",
                    callback_data=validate_callback_length(f"gen:setting:{setting_key}"),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=get_button_text("common.continue", lang), callback_data="gen:continue")])
    rows.append([InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="gen:back:models")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_setting_options_keyboard(model: Any, setting_key: str, current_value: str, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Построить клавиатуру вариантов значения настройки."""
    setting = model.user_settings[setting_key]
    rows = []
    option_buttons = []
    for option_index, (value, label) in enumerate(_get_setting_options(setting.options)):
        marker = "✅ " if value == current_value else ""
        display_label = get_setting_option_display_label(value, label, lang)
        option_buttons.append(
            InlineKeyboardButton(
                text=f"{marker}{display_label}",
                callback_data=validate_callback_length(f"gen:set:{setting_key}:{option_index}"),
            )
        )
    if setting_key == "num_generations":
        rows.extend(option_buttons[index:index + 2] for index in range(0, len(option_buttons), 2))
    else:
        rows.extend([button] for button in option_buttons)
    rows.append([InlineKeyboardButton(text=get_button_text("common.back_to_settings", lang), callback_data="gen:back:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_setting_input_back_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Back button for free-form setting input screens."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=get_button_text("common.back_to_settings", lang), callback_data="gen:back:settings")]]
    )


def build_generation_confirm_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Кнопки подтверждения запуска генерации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=get_button_text("generation.confirm", lang), callback_data="gen:confirm")],
            [InlineKeyboardButton(text=get_button_text("common.change_settings", lang), callback_data="gen:back:settings")],
        ]
    )


def build_insufficient_balance_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Inline-кнопки для сценария нехватки кредитов."""
    keyboard = build_error_keyboard("E006", lang)
    if keyboard is None:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=get_button_text("profile.top_up", lang), callback_data="profile:topup")],
                [InlineKeyboardButton(text=get_button_text("main.profile", lang), callback_data="profile:open")],
            ]
        )
    return keyboard


def build_back_to_settings_keyboard(lang: str = DEFAULT_LANGUAGE) -> ReplyKeyboardMarkup:
    """Совместимость со старым именем reply keyboard возврата к настройкам."""
    return build_back_to_settings_reply_keyboard(lang)


def build_generation_type_keyboard(options: Iterable[tuple[str, str]] | None = None, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора разделов."""
    if options is None:
        return build_generation_sections_keyboard(lang)

    rows = []
    for generation_type, label in options:
        callback_data = "gen:all" if generation_type in {"all", "all_models"} else f"gen:section:{generation_type}"
        rows.append([InlineKeyboardButton(text=label, callback_data=validate_callback_length(callback_data))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_provider_keyboard(providers: Iterable[tuple[str, str]] | None = None, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора провайдера."""
    if providers is None:
        return build_providers_keyboard(lang)

    provider_items = list(providers)
    return build_paginated_keyboard(
        provider_items,
        0,
        item_callback_builder=lambda provider_item: f"gen:provider:{provider_item[0]}:0",
        item_text_builder=lambda provider_item: provider_item[1],
        page_callback_builder=lambda target_page: f"gen:providers:{target_page}",
        back_callback="gen:back:sections",
        columns=2,
        lang=lang,
    )


def build_model_selection_keyboard(models: Iterable[Any], lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции выбора модели."""
    return build_models_keyboard(models, "gen:back:sections", lang)


def build_generation_type_selection_keyboard(options: Iterable[tuple[str, str]], lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора типа генерации."""
    return build_generation_type_keyboard(options, lang)


def build_provider_selection_keyboard(providers: Iterable[tuple[str, str]], lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Совместимость со старым именем клавиатуры выбора провайдера."""
    return build_provider_keyboard(providers, lang)


def get_generation_models_keyboard(models: Iterable[Any], lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции выбора модели."""
    return build_model_selection_keyboard(models, lang)


def get_generation_confirm_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Совместимость со старым именем функции подтверждения."""
    return build_generation_confirm_keyboard(lang)


def get_back_to_menu_inline_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Инлайн кнопка возврата в меню."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="back_to_menu")],
        ]
    )


def get_profile_keyboard(*, send_results_as_files: bool, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Инлайн-кнопки профиля пользователя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=get_button_text("profile.top_up", lang), callback_data="profile:top_up_balance")],
            [InlineKeyboardButton(text=get_button_text("profile.toggle_delivery", lang), callback_data="profile:toggle_delivery_mode")],
        ]
    )


def build_top_up_method_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Клавиатура выбора способа пополнения баланса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ {t('payments.telegram_stars', lang)}", callback_data="pay:method:stars")],
            [InlineKeyboardButton(text=t("payments.crypto", lang), callback_data="pay:crypto")],
            [InlineKeyboardButton(text=get_button_text("payments.back_to_profile", lang), callback_data="pay:back:profile")],
        ]
    )


def build_stars_top_up_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Клавиатура выбора пакета Telegram Stars."""
    builder = InlineKeyboardBuilder()
    for amount in ALLOWED_STARS_AMOUNTS:
        builder.button(
            text=f"{amount} ⭐",
            callback_data=validate_callback_length(f"pay:stars:{amount}"),
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text=get_button_text("common.back", lang),
            callback_data="pay:back:methods",
        )
    )
    return builder.as_markup()


def build_stars_wallet_redirect_keyboard(*, wallet_payment_url: str, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Клавиатура перехода во внешний Stars wallet bot."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("payments.open_stars_wallet", lang), url=wallet_payment_url)],
            [InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="pay:back:stars_amounts")],
        ]
    )


def build_wallet_bot_payment_keyboard(*, amount: int, wallet_payment_url: str, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Клавиатура перехода во внешний wallet bot для оплаты Stars."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("payments.open_wallet_bot", lang), url=wallet_payment_url)],
        ]
    )


def build_crypto_top_up_keyboard(lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Клавиатура выбора crypto-пакета кредитов."""
    builder = InlineKeyboardBuilder()
    for amount in ALLOWED_STARS_AMOUNTS:
        builder.button(
            text=t("payments.credit_amount", lang, amount=amount),
            callback_data=validate_callback_length(f"pay:crypto:{amount}"),
        )
    builder.adjust(2)
    builder.row(
        InlineKeyboardButton(
            text=get_button_text("common.back", lang),
            callback_data="pay:back:methods",
        )
    )
    return builder.as_markup()


def build_crypto_payment_keyboard(*, payment_url: str | None, lang: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Клавиатура оплаты crypto через NOWPayments."""
    rows = []
    if payment_url:
        rows.append([InlineKeyboardButton(text=t("payments.pay_with_nowpayments", lang), url=payment_url)])
    rows.append(
        [InlineKeyboardButton(text=get_button_text("common.back", lang), callback_data="pay:back:crypto_amounts")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
