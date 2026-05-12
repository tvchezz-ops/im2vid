"""Роутер профиля пользователя."""

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import get_main_menu_keyboard, get_profile_keyboard, is_localized_button_text
from app.bot.routers.generations import is_generation_flow_state, reset_generation_flow
from app.db import UserRepository
from app.i18n import get_user_language, t
from app.utils import logger


router = Router()


def format_delivery_mode(send_results_as_files: bool, lang: str = "en") -> str:
    return t("profile.delivery_file", lang) if send_results_as_files else t("profile.delivery_normal", lang)


def build_profile_text(user, total_spent_credits: int, lang: str = "en") -> str:
    return (
        f"👤 <b>{t('profile.title', lang)}</b>\n"
        f"💳 {t('profile.balance', lang)}: {user.balance}\n"
        f"📦 {t('profile.delivery_mode', lang)}: {format_delivery_mode(user.send_results_as_files, lang)}\n"
        f"🎨 {t('profile.total_generations', lang)}: {user.total_generations}\n"
        f"💰 {t('profile.credits_spent', lang)}: {total_spent_credits}"
    )


@router.message(lambda msg: is_localized_button_text(msg.text, "main.profile", getattr(msg.from_user, "language_code", None)))
async def show_profile(message: Message, state: FSMContext, session: AsyncSession):
    """Показать профиль пользователя."""
    try:
        current_state = await state.get_state()
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason="main_menu_profile")
            reset_lang = get_user_language(getattr(message.from_user, "language_code", None))
            await message.answer(
                t("generation.scenario_reset", reset_lang),
                reply_markup=get_main_menu_keyboard(reset_lang),
            )

        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        
        if not user:
            lang = get_user_language(getattr(message.from_user, "language_code", None))
            await message.answer(f"❌ {t('profile.user_not_found', lang)}")
            return
        
        lang = get_user_language(user.language_code)
        total_spent_credits = await user_repo.get_total_spent_credits(user.id)
        profile_text = build_profile_text(user, total_spent_credits, lang)
        
        await message.answer(
            profile_text,
            reply_markup=get_profile_keyboard(send_results_as_files=user.send_results_as_files, lang=lang),
            parse_mode="HTML",
        )
        
        logger.debug(f"Profile shown for user {message.from_user.id}")
    except Exception as e:
        logger.exception("Error in show_profile: %s", e)
        lang = get_user_language(getattr(message.from_user, "language_code", None))
        await message.answer(f"❌ {t('profile.load_error', lang)}")


@router.callback_query(lambda cb: cb.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    """Вернуться в главное меню."""
    lang = get_user_language(getattr(callback.from_user, "language_code", None))
    await callback.message.edit_text(
        t("profile.panel_closed", lang),
        reply_markup=None,
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "profile:open")
async def open_profile_callback(callback: CallbackQuery, session: AsyncSession):
    """Открыть профиль из inline-кнопок."""
    user_repo = UserRepository(session)
    user = await user_repo.get_or_create_user_from_telegram(callback.from_user)
    actor_lang = get_user_language(getattr(callback.from_user, "language_code", None))
    if user is None:
        await callback.answer(t("profile.user_not_found", actor_lang), show_alert=True)
        return
    lang = get_user_language(user.language_code)
    total_spent_credits = await user_repo.get_total_spent_credits(user.id)
    await callback.message.edit_text(
        build_profile_text(user, total_spent_credits, lang),
        reply_markup=get_profile_keyboard(send_results_as_files=user.send_results_as_files, lang=lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "profile:toggle_delivery_mode")
async def toggle_delivery_mode(callback: CallbackQuery, session: AsyncSession):
    """Переключить способ отправки результатов и обновить экран профиля."""
    user_repo = UserRepository(session)
    await user_repo.get_or_create_user_from_telegram(callback.from_user)
    new_value = await user_repo.toggle_user_delivery_preference(callback.from_user.id)
    user = await user_repo.get_user_profile(callback.from_user.id)
    actor_lang = get_user_language(getattr(callback.from_user, "language_code", None))
    if user is None:
        await callback.answer(t("profile.user_not_found", actor_lang), show_alert=True)
        return
    user.send_results_as_files = new_value
    lang = get_user_language(user.language_code)
    total_spent_credits = await user_repo.get_total_spent_credits(callback.from_user.id)
    await callback.message.edit_text(
        build_profile_text(user, total_spent_credits, lang),
        reply_markup=get_profile_keyboard(send_results_as_files=new_value, lang=lang),
        parse_mode="HTML",
    )
    await callback.answer(t("profile.setting_updated", lang))
