"""Роутер команды /start."""
from __future__ import annotations

from typing import Optional
from urllib.parse import unquote

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import get_button_text, get_main_menu_keyboard, get_profile_keyboard, is_localized_button_text
from app.bot.routers.generations import is_generation_flow_state, reset_generation_flow
from app.bot.routers.profile import build_profile_text
from app.db import PaymentProvider, UserRepository
from app.i18n import get_user_language, t
from app.services.payments import PaymentService
from app.utils import logger


router = Router()


def build_welcome_text(first_name: Optional[str], lang: str = "en") -> str:
    """Текст приветствия в главном меню."""
    display_name = first_name or t("main.welcome_friend", lang)
    return t("main.welcome", lang, display_name=display_name)


async def show_profile_after_payment_return(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Show the current profile after returning from an external payment flow."""
    await state.clear()
    user_repo = UserRepository(session)
    user = await user_repo.get_or_create_user_from_telegram(message.from_user)
    fresh_user = await user_repo.get_user_profile(user.id)
    profile_user = fresh_user or user
    lang = get_user_language(profile_user.language_code)
    total_spent_credits = await user_repo.get_total_spent_credits(profile_user.id)
    await message.answer(
        build_profile_text(profile_user, total_spent_credits, lang),
        reply_markup=get_profile_keyboard(send_results_as_files=profile_user.send_results_as_files, lang=lang),
        parse_mode="HTML",
    )


async def _show_payment_return_profile(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    action: str,
    order=None,
) -> None:
    log_payload = {
        "action": action,
        "user_id": message.from_user.id,
    }
    if order is not None:
        log_payload["order_id"] = str(order.id)
        log_payload["order_status"] = order.status
    logger.info(log_payload)
    await show_profile_after_payment_return(message, state, session)


@router.message(Command("start"))
async def start_command(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    command: CommandObject | None = None,
):
    """Обработчик команды /start."""
    try:
        current_state = await state.get_state()
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason="command_start")
            reset_lang = get_user_language(getattr(message.from_user, "language_code", None))
            await message.answer(
                t("generation.scenario_reset", reset_lang),
                reply_markup=get_main_menu_keyboard(reset_lang),
            )
        elif current_state is not None:
            await state.clear()

        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        lang = get_user_language(user.language_code)

        start_payload = (command.args or "").strip() if command is not None else ""
        if start_payload == "payment_success":
            await _show_payment_return_profile(message, state, session, action="payment_success_return_received")
            return

        if start_payload.startswith("paid_"):
            payment_payload = unquote(start_payload.removeprefix("paid_"))
            order = await PaymentService(session).payment_repo.get_payment_order_by_payload(payment_payload)
            if order is not None and order.provider == PaymentProvider.TELEGRAM_STARS.value and order.user_id == user.id:
                await _show_payment_return_profile(
                    message,
                    state,
                    session,
                    action="stars_wallet_paid_return_received",
                    order=order,
                )
                return
            if order is not None:
                await _show_payment_return_profile(
                    message,
                    state,
                    session,
                    action="stars_wallet_paid_return_ignored",
                    order=order,
                )
                return
            await _show_payment_return_profile(message, state, session, action="stars_wallet_paid_return_unknown")
            return

        if start_payload:
            order = await PaymentService(session).payment_repo.get_payment_order_by_payload(start_payload)
            if order is not None and order.provider == PaymentProvider.TELEGRAM_STARS.value and order.user_id == user.id:
                await _show_payment_return_profile(
                    message,
                    state,
                    session,
                    action="stars_wallet_return_received",
                    order=order,
                )
                return

            if order is not None:
                await _show_payment_return_profile(
                    message,
                    state,
                    session,
                    action="stars_wallet_return_ignored",
                    order=order,
                )
                return
            if start_payload.startswith("stars_"):
                await _show_payment_return_profile(message, state, session, action="legacy_stars_wallet_return_received")
                return
        
        await message.answer(
            build_welcome_text(user.first_name, lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
        
        logger.info(f"User {message.from_user.id} started the bot")
    except Exception as e:
        logger.exception("Error in start command: %s", e)
        lang = get_user_language(getattr(message.from_user, "language_code", None))
        await message.answer(t("main.start_error", lang))


@router.message(Command("menu"))
@router.message(Command("cancel"))
@router.message(lambda msg: is_localized_button_text(msg.text, "common.back", getattr(msg.from_user, "language_code", None)))
async def menu_command(message: Message, state: FSMContext, session: AsyncSession):
    """Вернуть пользователя в главное меню."""
    try:
        current_state = await state.get_state()
        user_repo = UserRepository(session)
        user = await user_repo.get_or_create_user_from_telegram(message.from_user)
        lang = get_user_language(user.language_code)
        if is_generation_flow_state(current_state):
            await state.update_data(last_user_id=message.from_user.id)
            await reset_generation_flow(state, reason=f"command_{(message.text or '').lstrip('/').lower() or 'menu_button'}")
            await message.answer(
                t("generation.scenario_reset", lang),
                reply_markup=get_main_menu_keyboard(lang),
            )
        elif current_state is not None:
            await state.clear()

        await message.answer(
            t("main.choose_section", lang),
            reply_markup=get_main_menu_keyboard(lang),
        )
    except Exception as e:
        logger.exception("Error in menu command: %s", e)
        lang = get_user_language(getattr(message.from_user, "language_code", None))
        await message.answer(t("main.menu_open_error", lang))
