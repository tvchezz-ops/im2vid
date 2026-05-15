"""Роутер профиля пользователя."""
from __future__ import annotations

from typing import Optional

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.error_messages import build_error_keyboard, build_user_error_message
from app.bot.keyboards import get_main_menu_keyboard, get_profile_keyboard, get_referral_keyboard, is_localized_button_text
from app.bot.language import get_event_lang
from app.bot.routers.generations import is_generation_flow_state, reset_generation_flow
from app.db import UserRepository
from app.config import settings
from app.i18n import t
from app.utils import logger


router = Router()
SUPPORT_USERNAME = "supbananify"


def format_delivery_mode(send_results_as_files: bool, lang: str = "en") -> str:
    return t("profile.delivery_status_file", lang) if send_results_as_files else t("profile.delivery_status_normal", lang)


def build_support_link(username: str = SUPPORT_USERNAME) -> str:
    normalized_username = username.strip().lstrip("@")
    return f'<a href="https://t.me/{normalized_username}">@{normalized_username}</a>'


def build_support_contact_text(lang: str = "en") -> str:
    return t("profile.support_contact", lang, support_link=build_support_link())


def build_profile_text(user, total_spent_credits: int, lang: str = "en", accepted_referrals_count: int = 0) -> str:
    return (
        f"👤 <b>{t('profile.title', lang)}</b>\n"
        "\n"
        f"💳 {t('profile.balance', lang)}: {user.balance}\n"
        f"🎨 {t('profile.total_generations', lang)}: {user.total_generations}\n"
        f"{build_support_contact_text(lang)}\n"
        f"🎁 {t('referral.invited_count', lang, count=accepted_referrals_count)}\n"
        f"📦 {t('profile.delivery_mode', lang)}: {format_delivery_mode(user.send_results_as_files, lang)}"
    )


def _normalize_bot_username(username: str | None) -> str:
    return (username or "").strip().lstrip("@")


def build_referral_link(bot_username: str, start_payload: str) -> str:
    return f"https://t.me/{_normalize_bot_username(bot_username)}?start={start_payload}"


def _get_referral_bot_username() -> str:
    return _normalize_bot_username(settings.main_bot_username) or _normalize_bot_username(settings.telegram_stars_return_bot_username)


def build_referral_text(referral_link: str, lang: str = "en") -> str:
    return (
        f"{t('profile.referral.title', lang)}\n\n"
        f"{t('profile.referral.description', lang)}\n\n"
        f"{t('profile.referral.link', lang)}\n"
        f"{referral_link}"
    )


@router.message(lambda msg: is_localized_button_text(msg.text, "main.profile", getattr(msg.from_user, "language_code", None)))
async def show_profile(message: Message, state: FSMContext, session: AsyncSession):
    """Показать профиль пользователя."""
    try:
        lang = await get_event_lang(message, session)
        current_state = await state.get_state()
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason="main_menu_profile")
            await message.answer(
                t("generation.scenario_reset", lang),
                reply_markup=get_main_menu_keyboard(lang),
            )

        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        
        if not user:
            await message.answer(build_user_error_message("profile.user_not_found", lang), reply_markup=build_error_keyboard("profile.user_not_found", lang))
            return
        
        total_spent_credits = await user_repo.get_total_spent_credits(user.id)
        accepted_referrals_count = await user_repo.count_accepted_referrals(user.id)
        profile_text = build_profile_text(user, total_spent_credits, lang, accepted_referrals_count)
        
        await message.answer(
            profile_text,
            reply_markup=get_profile_keyboard(
                send_results_as_files=user.send_results_as_files,
                lang=lang,
                referrals_enabled=settings.referral_enabled,
            ),
            parse_mode="HTML",
        )
        
        logger.debug(f"Profile shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_profile: %s", e)
        lang = await get_event_lang(message, session)
        await message.answer(build_user_error_message("profile.load_error", lang), reply_markup=build_error_keyboard("profile.load_error", lang))


@router.callback_query(lambda cb: cb.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, session: Optional[AsyncSession] = None):
    """Вернуться в главное меню."""
    lang = await get_event_lang(callback, session)
    await callback.message.edit_text(
        t("profile.panel_closed", lang),
        reply_markup=None,
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "profile:open")
async def open_profile_callback(callback: CallbackQuery, session: AsyncSession):
    """Открыть профиль из inline-кнопок."""
    user_repo = UserRepository(session)
    lang = await get_event_lang(callback, session)
    user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
    if user is None:
        await callback.answer(build_user_error_message("profile.user_not_found", lang), show_alert=True)
        return
    total_spent_credits = await user_repo.get_total_spent_credits(user.id)
    accepted_referrals_count = await user_repo.count_accepted_referrals(user.id)
    await callback.message.edit_text(
        build_profile_text(user, total_spent_credits, lang, accepted_referrals_count),
        reply_markup=get_profile_keyboard(
            send_results_as_files=user.send_results_as_files,
            lang=lang,
            referrals_enabled=settings.referral_enabled,
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "profile:invite_friends")
async def show_referral_invite(callback: CallbackQuery, session: AsyncSession):
    """Show the user's personal referral link."""
    if not settings.referral_enabled:
        await callback.answer()
        return

    user_repo = UserRepository(session)
    lang = await get_event_lang(callback, session)
    user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
    await user_repo.ensure_referral_code(user.id)
    start_payload = await user_repo.ensure_start_payload(user.id)
    if not start_payload:
        await callback.answer(build_user_error_message("profile.user_not_found", lang), show_alert=True)
        return

    referral_link = build_referral_link(_get_referral_bot_username(), start_payload)
    await callback.message.edit_text(
        build_referral_text(referral_link, lang),
        reply_markup=get_referral_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "profile:toggle_delivery_mode")
async def toggle_delivery_mode(callback: CallbackQuery, session: AsyncSession):
    """Переключить способ отправки результатов и обновить экран профиля."""
    user_repo = UserRepository(session)
    lang = await get_event_lang(callback, session)
    await user_repo.get_or_create_user_from_telegram(callback.from_user)
    new_value = await user_repo.toggle_user_delivery_preference(callback.from_user.id)
    user = await user_repo.get_user_profile(callback.from_user.id)
    if user is None:
        await callback.answer(build_user_error_message("profile.user_not_found", lang), show_alert=True)
        return
    user.send_results_as_files = new_value
    total_spent_credits = await user_repo.get_total_spent_credits(callback.from_user.id)
    accepted_referrals_count = await user_repo.count_accepted_referrals(callback.from_user.id)
    await callback.message.edit_text(
        build_profile_text(user, total_spent_credits, lang, accepted_referrals_count),
        reply_markup=get_profile_keyboard(
            send_results_as_files=new_value,
            lang=lang,
            referrals_enabled=settings.referral_enabled,
        ),
        parse_mode="HTML",
    )
    await callback.answer(t("profile.setting_updated", lang))
