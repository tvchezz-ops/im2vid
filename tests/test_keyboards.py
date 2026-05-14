"""Tests for generation selection keyboards."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.bot.keyboards import (
    build_back_to_settings_reply_keyboard,
    build_generation_confirm_keyboard,
    build_generation_sections_keyboard,
    build_generation_summary_keyboard,
    build_main_menu_keyboard,
    build_model_settings_keyboard,
    build_models_keyboard,
    build_paginated_keyboard,
    build_providers_keyboard,
    build_crypto_top_up_keyboard,
    build_setting_input_back_keyboard,
    build_setting_options_keyboard,
    build_stars_wallet_redirect_keyboard,
    build_stars_top_up_keyboard,
    build_top_up_method_keyboard,
    build_wallet_bot_payment_keyboard,
    get_button_text,
    resolve_model_key_from_token,
    validate_callback_length,
)
from app.services.generation_service import list_models_by_provider, list_models_by_type
from app.i18n import t
from app.services.generation_service import get_generation_model
from app.services.generation_service import list_generation_models


def _iter_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _iter_callback_data(markup) -> list[str]:
    return [button.callback_data for button in _iter_buttons(markup) if button.callback_data is not None]


def test_build_generation_summary_keyboard_contains_only_repeat_button() -> None:
    keyboard = build_generation_summary_keyboard("12345678-1234-1234-1234-123456789abc", "en")

    assert len(keyboard.inline_keyboard) == 1
    assert len(keyboard.inline_keyboard[0]) == 1
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "🔁 Repeat"
    assert button.callback_data == "gen:repeat:12345678-1234-1234-1234-123456789abc"
    validate_callback_length(button.callback_data)


def test_build_generation_sections_keyboard_uses_expected_callback_prefix() -> None:
    keyboard = build_generation_sections_keyboard()

    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]
    all_button = keyboard.inline_keyboard[-1][0]

    callback_data = [button.callback_data for button in buttons]
    assert "gen:section:image_edit" in callback_data
    assert "gen:section:text_to_video" in callback_data
    assert "gen:section:reference_to_video" in callback_data
    assert "gen:section:video_extend" in callback_data
    assert "gen:section:lipsync" in callback_data
    assert "gen:section:video_to_audio" in callback_data
    assert "gen:section:effects" not in callback_data
    assert all_button.callback_data == "gen:all"


def test_build_generation_sections_keyboard_uses_only_section_callbacks_for_categories() -> None:
    keyboard = build_generation_sections_keyboard()

    callback_data = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "gen:section:all_models" not in callback_data
    assert callback_data[-1] == "gen:all"


def test_build_generation_sections_keyboard_always_shows_all_models(monkeypatch) -> None:
    import app.bot.keyboards as keyboards

    monkeypatch.setattr(keyboards, "list_generation_types", lambda: [])

    keyboard = build_generation_sections_keyboard()

    assert [button.callback_data for row in keyboard.inline_keyboard for button in row] == ["gen:all"]


def test_build_providers_keyboard_uses_expected_callback_prefix() -> None:
    keyboard = build_providers_keyboard()

    callback_data = _iter_callback_data(keyboard)
    button_texts = [button.text for button in _iter_buttons(keyboard)]
    assert "gen:provider:google:0" in callback_data
    assert "gen:provider:bytedance:0" in callback_data
    assert "gen:provider:kling:0" in callback_data
    assert "gen:provider:grok:0" in callback_data
    assert "gen:provider:minimax:0" in callback_data
    assert "gen:provider:wavespeed_ai:0" in callback_data
    assert "gen:provider:midjourney" not in callback_data
    assert "Wan AI" in button_texts
    assert all("Wavespeed" not in button.text for button in _iter_buttons(keyboard))


def test_image_upscaler_is_user_visible_under_image_to_image_and_wan_ai() -> None:
    category_models = list_models_by_type("image_to_image")
    provider_models = list_models_by_provider("wavespeed_ai")
    category_index = next(index for index, model in enumerate(category_models) if model.key == "wan_ai_image_upscaler")
    provider_index = next(index for index, model in enumerate(provider_models) if model.key == "wan_ai_image_upscaler")

    assert any(model.key == "wan_ai_image_upscaler" for model in category_models)
    assert any(model.key == "wan_ai_image_upscaler" for model in provider_models)

    category_keyboard = build_models_keyboard(category_models, "gen:back:sections", page=category_index // 8)
    provider_keyboard = build_models_keyboard(provider_models, "gen:back:providers", page=provider_index // 8)
    category_texts = [button.text for button in _iter_buttons(category_keyboard)]
    provider_texts = [button.text for button in _iter_buttons(provider_keyboard)]

    assert "Image Upscaler" in category_texts
    assert "Image Upscaler" in provider_texts
    assert all("Wavespeed" not in text for text in category_texts + provider_texts)


def test_image_upscaler_settings_keyboard_uses_localized_labels() -> None:
    model = get_generation_model("wan_ai_image_upscaler")

    settings_keyboard = build_model_settings_keyboard(model, {"target_resolution": "4k", "output_format": "jpeg"}, lang="ru")
    settings_texts = [button.text for button in _iter_buttons(settings_keyboard)]

    assert "Целевое разрешение: 4k" in settings_texts
    assert "Формат файла: jpeg" in settings_texts

    options_keyboard = build_setting_options_keyboard(model, "output_format", "jpeg", lang="ru")
    option_texts = [button.text for button in _iter_buttons(options_keyboard)]

    assert "✅ jpeg" in option_texts
    assert "png" in option_texts
    assert "webp" in option_texts


def test_build_models_keyboard_uses_passed_models_only() -> None:
    models = [
        SimpleNamespace(key="nano_banana", title="Nano Banana Pro Edit Ultra"),
        SimpleNamespace(key="seedream", title="Seedream V4.5 Edit"),
    ]

    keyboard = build_models_keyboard(models, "gen:back:sections")
    buttons = [row[0] for row in keyboard.inline_keyboard[:-1]]

    assert [button.text for button in buttons] == [
        "Nano Banana Pro Edit Ultra",
        "Seedream V4.5 Edit",
    ]
    assert [button.callback_data for button in buttons] == [
        "gen:model:nano_banana",
        "gen:model:seedream",
    ]


def test_build_models_keyboard_hides_model_price() -> None:
    keyboard = build_models_keyboard([get_generation_model("nano_banana")], "gen:back:sections")

    assert keyboard.inline_keyboard[0][0].text == "Google Nano Banana Pro Edit Ultra"
    assert "credits" not in keyboard.inline_keyboard[0][0].text


def test_build_models_keyboard_hides_minimum_video_duration_price_in_ru() -> None:
    keyboard = build_models_keyboard([get_generation_model("alibaba_wan_2_6_text_to_video")], "gen:back:sections", "ru")

    assert keyboard.inline_keyboard[0][0].text == "Alibaba Wan 2.6 Text To Video"
    assert "credits" not in keyboard.inline_keyboard[0][0].text


def test_build_models_keyboard_hides_estimated_fallback_price() -> None:
    keyboard = build_models_keyboard([get_generation_model("alibaba_wan_2_6_text_to_image")], "gen:back:sections")

    assert keyboard.inline_keyboard[0][0].text == "Alibaba Wan 2.6 Text To Image"
    assert "credits" not in keyboard.inline_keyboard[0][0].text


def test_build_models_keyboard_falls_back_to_index_for_long_model_key() -> None:
    long_key = "m" * 80
    models = [SimpleNamespace(key=long_key, title="Very Long Model")]

    keyboard = build_models_keyboard(models, "gen:back:sections")

    assert keyboard.inline_keyboard[0][0].callback_data == "gen:model:i0"
    assert resolve_model_key_from_token(models, "i0") == long_key


def test_build_paginated_keyboard_first_page() -> None:
    items = [f"Item {index}" for index in range(10)]

    keyboard = build_paginated_keyboard(
        items,
        0,
        item_callback_builder=lambda item, index: f"item:{index}",
        page_callback_builder=lambda page: f"page:{page}",
        back_callback="back",
    )

    assert [row[0].text for row in keyboard.inline_keyboard[:8]] == items[:8]
    assert [button.callback_data for button in keyboard.inline_keyboard[-2]] == ["gen:page:noop", "page:1"]
    assert keyboard.inline_keyboard[-2][0].text == "Page 1/2"
    assert all(button.text != "⬅️ Prev" for button in keyboard.inline_keyboard[-2])


def test_build_paginated_keyboard_next_page() -> None:
    keyboard = build_paginated_keyboard(
        [f"Item {index}" for index in range(10)],
        0,
        item_callback_builder=lambda item, index: f"item:{index}",
        page_callback_builder=lambda page: f"page:{page}",
        back_callback="back",
    )

    assert keyboard.inline_keyboard[-2][-1].callback_data == "page:1"


def test_build_paginated_keyboard_prev_page() -> None:
    keyboard = build_paginated_keyboard(
        [f"Item {index}" for index in range(10)],
        1,
        item_callback_builder=lambda item, index: f"item:{index}",
        page_callback_builder=lambda page: f"page:{page}",
        back_callback="back",
    )

    assert [row[0].text for row in keyboard.inline_keyboard[:2]] == ["Item 8", "Item 9"]
    assert keyboard.inline_keyboard[-2][0].callback_data == "page:0"
    assert keyboard.inline_keyboard[-2][1].text == "Page 2/2"
    assert all(button.text != "Next ➡️" for button in keyboard.inline_keyboard[-2])


def test_build_paginated_keyboard_middle_page_has_prev_and_next() -> None:
    keyboard = build_paginated_keyboard(
        [f"Item {index}" for index in range(17)],
        1,
        item_callback_builder=lambda item, index: f"item:{index}",
        page_callback_builder=lambda page: f"page:{page}",
        back_callback="back",
    )

    assert keyboard.inline_keyboard[-2][0].text == t("pagination.prev", "en")
    assert keyboard.inline_keyboard[-2][0].callback_data == "page:0"
    assert keyboard.inline_keyboard[-2][1].text == "Page 2/3"
    assert keyboard.inline_keyboard[-2][2].text == "Next ➡️"
    assert keyboard.inline_keyboard[-2][2].callback_data == "page:2"


def test_build_paginated_keyboard_last_page() -> None:
    keyboard = build_paginated_keyboard(
        [f"Item {index}" for index in range(17)],
        2,
        item_callback_builder=lambda item, index: f"item:{index}",
        page_callback_builder=lambda page: f"page:{page}",
        back_callback="back",
    )

    assert keyboard.inline_keyboard[0][0].text == "Item 16"
    assert keyboard.inline_keyboard[-2][0].callback_data == "page:1"
    assert keyboard.inline_keyboard[-2][1].text == "Page 3/3"
    assert all(button.text != "Next ➡️" for button in keyboard.inline_keyboard[-2])


def test_build_paginated_keyboard_single_page_has_no_navigation_row() -> None:
    keyboard = build_paginated_keyboard(
        ["Item 0", "Item 1"],
        0,
        item_callback_builder=lambda item, index: f"item:{index}",
        page_callback_builder=lambda page: f"page:{page}",
        back_callback="back",
    )

    assert len(keyboard.inline_keyboard) == 3
    assert keyboard.inline_keyboard[-1][0].callback_data == "back"
    assert all("Page" not in button.text for button in _iter_buttons(keyboard))


def test_paginated_keyboard_uses_i18n_text() -> None:
    keyboard = build_paginated_keyboard(
        [f"Item {index}" for index in range(17)],
        1,
        item_callback_builder=lambda item, index: f"item:{index}",
        page_callback_builder=lambda page: f"page:{page}",
        back_callback="back",
        lang="ru",
    )

    assert keyboard.inline_keyboard[-2][0].text == t("pagination.prev", "ru")
    assert keyboard.inline_keyboard[-2][1].text == t("pagination.page", "ru", current=2, total=3)
    assert keyboard.inline_keyboard[-2][2].text == t("pagination.next", "ru")
    assert keyboard.inline_keyboard[-1][0].text == get_button_text("common.back", "ru")


def test_build_paginated_keyboard_rejects_long_callback_data() -> None:
    with pytest.raises(ValueError, match="callback_data is too long"):
        build_paginated_keyboard(
            ["Item"],
            0,
            item_callback_builder=lambda item, index: "x" * 64,
            back_callback="back",
        )


def test_all_keyboard_callback_data_fit_telegram_limit() -> None:
    enabled_models = list_generation_models()
    model_pages = (len(enabled_models) + 7) // 8
    markups = [
        build_generation_sections_keyboard(),
        build_providers_keyboard(),
        build_model_settings_keyboard(
            get_generation_model("nano_banana"),
            {"aspect_ratio": "1:1", "resolution": "4k", "output_format": "png"},
        ),
        build_setting_options_keyboard(
            get_generation_model("nano_banana"),
            "aspect_ratio",
            "1:1",
        ),
        build_generation_confirm_keyboard(),
    ]
    markups.extend(
        build_models_keyboard(
            enabled_models,
            "gen:back:sections",
            page=page,
            page_callback_builder=lambda target_page: f"gen:models:all_models:{target_page}",
        )
        for page in range(model_pages)
    )

    for markup in markups:
        for callback_data in _iter_callback_data(markup):
            assert len(callback_data.encode("utf-8")) < 64


def test_all_models_provider_pagination_works(monkeypatch) -> None:
    import app.bot.keyboards as keyboards

    monkeypatch.setattr(keyboards, "list_providers", lambda: [f"provider_{index}" for index in range(10)])

    first_page = build_providers_keyboard(page=0)
    second_page = build_providers_keyboard(page=1)

    assert "gen:provider:provider_0:0" in _iter_callback_data(first_page)
    assert "gen:provider:provider_7:0" in _iter_callback_data(first_page)
    assert "gen:provider:provider_8:0" not in _iter_callback_data(first_page)
    assert first_page.inline_keyboard[-2][0].text == "Page 1/2"
    assert first_page.inline_keyboard[-2][1].callback_data == "gen:providers:1"
    assert all(button.text != "⬅️ Prev" for button in first_page.inline_keyboard[-2])
    assert "gen:provider:provider_8:0" in _iter_callback_data(second_page)
    assert "gen:provider:provider_9:0" in _iter_callback_data(second_page)
    assert second_page.inline_keyboard[-2][0].callback_data == "gen:providers:0"
    assert all(button.text != "Next ➡️" for button in second_page.inline_keyboard[-2])


def test_model_pagination_works_for_more_than_eight_models() -> None:
    models = list_generation_models()[:9]

    first_page = build_models_keyboard(
        models,
        "gen:back:sections",
        page=0,
        page_callback_builder=lambda target_page: f"gen:models:text_to_image:{target_page}",
    )
    second_page = build_models_keyboard(
        models,
        "gen:back:sections",
        page=1,
        page_callback_builder=lambda target_page: f"gen:models:text_to_image:{target_page}",
    )

    assert models[0].title in first_page.inline_keyboard[0][0].text
    assert models[7].title in first_page.inline_keyboard[7][0].text
    assert f"gen:model:{models[8].key}" not in _iter_callback_data(first_page)
    assert models[8].title in second_page.inline_keyboard[0][0].text
    assert first_page.inline_keyboard[-2][1].callback_data == "gen:models:text_to_image:1"
    assert second_page.inline_keyboard[-2][0].callback_data == "gen:models:text_to_image:0"


def test_model_keyboard_has_max_eight_single_button_model_rows_per_page() -> None:
    models = list_generation_models()[:9]

    first_page = build_models_keyboard(models, "gen:back:sections", page=0)
    second_page = build_models_keyboard(models, "gen:back:sections", page=1)

    assert len(first_page.inline_keyboard[:-2]) == 8
    assert all(len(row) == 1 for row in first_page.inline_keyboard[:-2])
    assert len(second_page.inline_keyboard[:-2]) == 1
    assert all(len(row) == 1 for row in second_page.inline_keyboard[:-2])


def test_model_keyboard_does_not_contain_credits_for_any_enabled_model() -> None:
    enabled_models = list_generation_models()
    page_count = (len(enabled_models) + 7) // 8

    for page in range(page_count):
        keyboard = build_models_keyboard(enabled_models, "gen:back:sections", page=page)
        for row in keyboard.inline_keyboard[:-2]:
            assert len(row) == 1
            assert "credits" not in row[0].text
            assert "—" not in row[0].text


def test_validate_callback_length_rejects_64_byte_callback() -> None:
    with pytest.raises(ValueError, match="callback_data is too long"):
        validate_callback_length("x" * 64)


def test_build_model_settings_keyboard_uses_setting_keys() -> None:
    model = SimpleNamespace(
        user_settings={
            "aspect_ratio": SimpleNamespace(key="aspect_ratio", title="Формат", default="1:1"),
        }
    )

    keyboard = build_model_settings_keyboard(model, {"aspect_ratio": "16:9"})

    assert keyboard.inline_keyboard[0][0].callback_data == "gen:setting:aspect_ratio"
    assert keyboard.inline_keyboard[-1][0].callback_data == "gen:back:models"


def test_build_model_settings_keyboard_shows_generated_settings_for_target_models() -> None:
    wan_keyboard = build_model_settings_keyboard(
        get_generation_model("alibaba_wan_2_6_image_to_video_flash"),
        {},
    )
    lipsync_keyboard = build_model_settings_keyboard(
        get_generation_model("kwaivgi_kling_lipsync_audio_to_video"),
        {},
    )

    wan_callbacks = _iter_callback_data(wan_keyboard)
    lipsync_callbacks = _iter_callback_data(lipsync_keyboard)
    lipsync_texts = [button.text for button in _iter_buttons(lipsync_keyboard)]

    assert "gen:setting:duration" in wan_callbacks
    assert "gen:setting:resolution" in wan_callbacks
    assert "gen:setting:shot_type" in wan_callbacks
    assert "gen:setting:num_generations" in wan_callbacks
    assert "gen:setting:audio" not in lipsync_callbacks
    assert all("Audio" not in text and "Аудио" not in text for text in lipsync_texts)
    assert "gen:setting:num_generations" in lipsync_callbacks


def test_build_setting_options_keyboard_uses_setting_key_and_option_index() -> None:
    model = SimpleNamespace(
        user_settings={
            "aspect_ratio": SimpleNamespace(
                options=[
                    SimpleNamespace(value="1:1", label="1:1"),
                    SimpleNamespace(value="16:9", label="16:9"),
                ]
            )
        }
    )

    keyboard = build_setting_options_keyboard(model, "aspect_ratio", "16:9")

    assert keyboard.inline_keyboard[0][0].callback_data == "gen:set:aspect_ratio:0"
    assert keyboard.inline_keyboard[1][0].text == "✅ 16:9"
    assert keyboard.inline_keyboard[-1][0].callback_data == "gen:back:settings"


def test_build_setting_options_keyboard_shows_num_generations_in_two_columns() -> None:
    model = get_generation_model("nano_banana")

    keyboard = build_setting_options_keyboard(model, "num_generations", "10")

    option_rows = keyboard.inline_keyboard[:-1]
    assert [[button.text for button in row] for row in option_rows] == [
        ["1", "2"],
        ["3", "4"],
        ["5", "6"],
        ["7", "8"],
        ["9", "✅ 10"],
    ]
    assert keyboard.inline_keyboard[-1][0].callback_data == "gen:back:settings"
    assert all(
        len((button.callback_data or "").encode("utf-8")) < 64
        for row in keyboard.inline_keyboard
        for button in row
    )


def test_build_setting_input_back_keyboard_uses_existing_callback() -> None:
    keyboard = build_setting_input_back_keyboard("ru")

    assert keyboard.inline_keyboard[0][0].text == f"⬅️ {t('common.back_to_settings', 'ru')}"
    assert keyboard.inline_keyboard[0][0].callback_data == "gen:back:settings"


def test_build_generation_confirm_keyboard_uses_new_callbacks() -> None:
    keyboard = build_generation_confirm_keyboard()

    assert keyboard.inline_keyboard[0][0].callback_data == "gen:confirm"
    assert keyboard.inline_keyboard[1][0].callback_data == "gen:back:settings"


def test_build_back_to_settings_reply_keyboard_uses_expected_text() -> None:
    keyboard = build_back_to_settings_reply_keyboard("ru")

    assert keyboard.keyboard[0][0].text == f"⬅️ {t('generation.back_to_settings', 'ru')}"


def test_build_main_menu_keyboard_uses_expected_layout() -> None:
    keyboard = build_main_menu_keyboard("ru")

    assert keyboard.keyboard[0][0].text == "🎨 Генерации"
    assert keyboard.keyboard[0][1].text == "👤 Профиль"
    assert len(keyboard.keyboard) == 1
    assert all(button.text != "🛒 Магазин" for row in keyboard.keyboard for button in row)
    assert keyboard.resize_keyboard is True
    assert keyboard.one_time_keyboard is False
    assert keyboard.input_field_placeholder == "Выберите раздел"


def test_build_profile_keyboard_has_no_history_button() -> None:
    from app.bot.keyboards import get_profile_keyboard

    keyboard = get_profile_keyboard(send_results_as_files=False, lang="ru")
    button_texts = [button.text for row in keyboard.inline_keyboard for button in row]

    assert button_texts == [f"💳 {t('profile.top_up', 'ru')}", f"⚙️ {t('profile.toggle_delivery', 'ru')}"]
    assert "📜 История генераций" not in button_texts


def test_build_profile_keyboard_has_no_back_button_in_ru_or_en() -> None:
    from app.bot.keyboards import get_profile_keyboard

    ru_keyboard = get_profile_keyboard(send_results_as_files=False, lang="ru")
    en_keyboard = get_profile_keyboard(send_results_as_files=False, lang="en")
    ru_button_texts = [button.text for row in ru_keyboard.inline_keyboard for button in row]
    en_button_texts = [button.text for row in en_keyboard.inline_keyboard for button in row]

    assert "Назад" not in " ".join(ru_button_texts)
    assert "Back" not in " ".join(en_button_texts)


def test_build_stars_top_up_keyboard_uses_expected_amount_callbacks() -> None:
    keyboard = build_stars_top_up_keyboard("ru")
    rows = keyboard.inline_keyboard
    buttons = [button for row in rows for button in row]

    assert [button.text for button in buttons] == [
        "100 ⭐",
        "300 ⭐",
        "500 ⭐",
        "1000 ⭐",
        "3000 ⭐",
        "5000 ⭐",
        "⬅️ Назад",
    ]
    assert [button.callback_data for button in buttons] == [
        "pay:stars:100",
        "pay:stars:300",
        "pay:stars:500",
        "pay:stars:1000",
        "pay:stars:3000",
        "pay:stars:5000",
        "pay:back:methods",
    ]
    assert all("Магазин" not in button.text for button in buttons)


def test_build_stars_top_up_keyboard_uses_two_column_amount_layout() -> None:
    keyboard = build_stars_top_up_keyboard("ru")
    rows = keyboard.inline_keyboard

    assert len(rows) == 4
    assert [len(row) for row in rows[:3]] == [2, 2, 2]
    assert [button.text for row in rows[:3] for button in row] == [
        "100 ⭐",
        "300 ⭐",
        "500 ⭐",
        "1000 ⭐",
        "3000 ⭐",
        "5000 ⭐",
    ]
    assert len(rows[-1]) == 1
    assert rows[-1][0].text == "⬅️ Назад"
    assert rows[-1][0].callback_data == "pay:back:methods"


def test_build_crypto_top_up_keyboard_uses_two_column_amount_layout() -> None:
    keyboard = build_crypto_top_up_keyboard("ru")
    rows = keyboard.inline_keyboard

    assert len(rows) == 4
    assert [len(row) for row in rows[:3]] == [2, 2, 2]
    assert [button.text for row in rows[:3] for button in row] == [
        t("payments.credit_amount", "ru", amount=100),
        t("payments.credit_amount", "ru", amount=300),
        t("payments.credit_amount", "ru", amount=500),
        t("payments.credit_amount", "ru", amount=1000),
        t("payments.credit_amount", "ru", amount=3000),
        t("payments.credit_amount", "ru", amount=5000),
    ]
    assert [button.callback_data for row in rows[:3] for button in row] == [
        "pay:crypto:100",
        "pay:crypto:300",
        "pay:crypto:500",
        "pay:crypto:1000",
        "pay:crypto:3000",
        "pay:crypto:5000",
    ]
    assert len(rows[-1]) == 1
    assert rows[-1][0].text == "⬅️ Назад"
    assert rows[-1][0].callback_data == "pay:back:methods"


def test_build_top_up_method_keyboard_uses_payment_methods() -> None:
    keyboard = build_top_up_method_keyboard("ru")
    buttons = [row[0] for row in keyboard.inline_keyboard]

    assert [button.text for button in buttons] == ["⭐ Telegram Stars", "₿ Crypto", f"⬅️ {t('main.profile', 'ru')}"]
    assert [button.callback_data for button in buttons] == ["pay:method:stars", "pay:crypto", "pay:back:profile"]


def test_build_stars_wallet_redirect_keyboard_uses_single_wallet_url() -> None:
    keyboard = build_stars_wallet_redirect_keyboard(
        wallet_payment_url="https://t.me/wallet_bot?start=stars_token",
        lang="ru",
    )

    assert keyboard.inline_keyboard[0][0].text == f"{t('payments.open_wallet_bot', 'ru')} ⭐"
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/wallet_bot?start=stars_token"
    assert keyboard.inline_keyboard[1][0].text == "⬅️ Назад"
    assert keyboard.inline_keyboard[1][0].callback_data == "pay:back:stars_amounts"


def test_build_wallet_bot_payment_keyboard_uses_single_pay_url_button() -> None:
    keyboard = build_wallet_bot_payment_keyboard(
        amount=500,
        wallet_payment_url="https://t.me/wallet_bot?start=500credits",
    )

    assert len(keyboard.inline_keyboard) == 1
    assert keyboard.inline_keyboard[0][0].text == t("payments.open_wallet_bot", "en")
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/wallet_bot?start=500credits"
