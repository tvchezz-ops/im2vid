"""Роутер команды /start."""
from __future__ import annotations

from typing import Optional
import re
from urllib.parse import unquote

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.error_messages import build_error_keyboard, build_user_error_message, log_error_code
from app.bot.keyboards import get_main_menu_keyboard, is_localized_button_text
from app.bot.routers.generations import is_generation_flow_state, reset_generation_flow
from app.bot.routers.profile import build_profile_text
from app.config import settings
from app.db import PaymentOrderStatus, PaymentProvider, UserRepository
from app.i18n import get_user_language, t
from app.services.payments import PaymentService
from app.services.referrals import ReferralService
from app.utils import logger
from app.utils.referrals import mask_start_payload


router = Router()

_START_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_RESERVED_START_PAYLOADS = {"payment_success"}
_RESERVED_START_PREFIXES = ("paid_", "stars_", "pay_")


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
    accepted_referrals_count = await user_repo.count_accepted_referrals(profile_user.id)
    await message.answer(
        build_profile_text(profile_user, total_spent_credits, lang, accepted_referrals_count),
        reply_markup=get_main_menu_keyboard(lang),
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


def _extract_start_payload_from_text(text: str | None) -> str:
    if not text:
        return ""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or parts[0].split("@", 1)[0] != "/start":
        return ""
    return parts[1].strip()


def _get_start_payload(command: CommandObject | None, message: Message) -> str:
    if command is not None and command.args:
        return command.args.strip()
    return _extract_start_payload_from_text(message.text)


def _has_start_payload(message: Message) -> bool:
    return bool(_extract_start_payload_from_text(message.text))


def _payment_payload_candidates(start_payload: str) -> list[str]:
    raw_payload = start_payload.strip()
    candidates: list[str] = []
    if raw_payload.startswith("paid_"):
        candidates.append(unquote(raw_payload.removeprefix("paid_")))
    if raw_payload.startswith("stars_"):
        candidates.append(unquote(raw_payload.removeprefix("stars_")))
    candidates.append(unquote(raw_payload))

    unique_candidates: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique_candidates:
            unique_candidates.append(candidate)
    return unique_candidates


def _extract_legacy_referral_code(start_payload: str) -> str | None:
    payload = start_payload.strip()
    if not payload.startswith("ref_"):
        return None
    referral_code = payload.removeprefix("ref_").strip()
    return referral_code or None


def _is_reserved_start_payload(start_payload: str) -> bool:
    payload = start_payload.strip()
    return payload in _RESERVED_START_PAYLOADS or payload.startswith(_RESERVED_START_PREFIXES)


def _is_valid_referral_start_payload(start_payload: str) -> bool:
    payload = start_payload.strip()
    return bool(payload) and len(payload) <= 64 and bool(_START_PAYLOAD_RE.fullmatch(payload))


async def _resolve_referral_start_payload(session: AsyncSession, start_payload: str):
    payload = start_payload.strip()
    legacy_referral_code = _extract_legacy_referral_code(payload)
    if legacy_referral_code is not None:
        if len(legacy_referral_code) > 64:
            logger.info(
                {
                    "action": "referral_payload_rejected",
                    "reason": "oversized_payload",
                    "payload_prefix": mask_start_payload(payload),
                }
            )
            return None, None
        return legacy_referral_code, None

    if _is_reserved_start_payload(payload):
        return None, None

    if len(payload) > 64:
        logger.info(
            {
                "action": "referral_payload_rejected",
                "reason": "oversized_payload",
                "payload_prefix": mask_start_payload(payload),
            }
        )
        return None, None

    if not _is_valid_referral_start_payload(payload):
        logger.info(
            {
                "action": "referral_payload_rejected",
                "reason": "malformed_payload",
                "payload_prefix": mask_start_payload(payload),
            }
        )
        return None, None

    referrer = await UserRepository(session).get_user_by_start_payload(payload)
    return payload, referrer


_extract_referral_code = _extract_legacy_referral_code


async def _get_stars_wallet_return_order(session: AsyncSession, user_id: int, start_payload: str):
    payment_repo = PaymentService(session).payment_repo
    for payment_payload in _payment_payload_candidates(start_payload):
        order = await payment_repo.get_payment_order_by_payload(payment_payload)
        if (
            order is not None
            and order.provider == PaymentProvider.TELEGRAM_STARS.value
            and order.user_id == user_id
        ):
            return order
    return None


async def _clear_start_state_without_user_notice(message: Message, state: FSMContext, reason: str) -> None:
    current_state = await state.get_state()
    if is_generation_flow_state(current_state):
        await state.update_data(last_user_id=message.from_user.id)
        await reset_generation_flow(state, reason=reason)
    elif current_state is not None:
        await state.clear()


async def _send_start_welcome(message: Message, user, lang: str) -> None:
    await message.answer(
        build_welcome_text(user.first_name, lang),
        reply_markup=get_main_menu_keyboard(lang),
    )
    logger.info(f"User {message.from_user.id} started the bot")


async def _send_referral_start_welcome(message: Message, user, lang: str, *, accepted: bool, bonus_credits: int = 0) -> None:
    welcome_text = build_welcome_text(user.first_name, lang)
    if accepted:
        referral_lines = ["🎁 Реферальная ссылка применена."]
        if bonus_credits > 0:
            referral_lines.append(t("referral.bonus_added", lang, credits=bonus_credits))
        referral_text = "\n".join(referral_lines)
        welcome_text = f"{referral_text}\n\n{welcome_text}"
    await message.answer(welcome_text, reply_markup=get_main_menu_keyboard(lang))
    logger.info(f"User {message.from_user.id} started the bot")


@router.message(Command("start"), _has_start_payload)
async def start_payment_return_command(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    command: CommandObject | None = None,
):
    """Priority /start handler for external payment return payloads."""
    try:
        await _clear_start_state_without_user_notice(message, state, reason="payment_return_start")

        user_repo = UserRepository(session)
        user_result = await user_repo.ensure_user_from_telegram(message.from_user)
        user = user_result.user
        lang = get_user_language(user.language_code)

        start_payload = _get_start_payload(command, message)
        if start_payload == "payment_success":
            await _show_payment_return_profile(message, state, session, action="payment_success_return_received")
            return

        order = await _get_stars_wallet_return_order(session, user.id, start_payload)
        if order is None:
            if not settings.referral_enabled:
                await _send_start_welcome(message, user, lang)
                return

            referral_code, referrer = await _resolve_referral_start_payload(session, start_payload)
            if referral_code is None:
                await _send_start_welcome(message, user, lang)
                return

            referral_result = await ReferralService(session).apply_referral(
                user,
                referral_code,
                created=user_result.created,
                referrer=referrer,
            )
            await _send_referral_start_welcome(
                message,
                user,
                lang,
                accepted=referral_result.status == "accepted",
                bonus_credits=referral_result.referred_bonus_credits,
            )
            return

        await show_profile_after_payment_return(message, state, session)
        logger.info(
            {
                "action": "stars_wallet_return_profile_shown",
                "user_id": user.id,
                "order_id": str(order.id),
                "order_status": order.status,
            }
        )
        if order.status != PaymentOrderStatus.PAID.value:
            logger.info(
                {
                    "action": "stars_wallet_return_pending",
                    "user_id": user.id,
                    "order_id": str(order.id),
                    "order_status": order.status,
                }
            )
    except Exception as e:
        logger.exception("Error in payment return start command: %s", e)
        log_error_code("E010", {"action": "payment_return_start_error", "error": e.__class__.__name__})
        lang = get_user_language(getattr(message.from_user, "language_code", None))
        await message.answer(build_user_error_message("main.start_error", lang), reply_markup=build_error_keyboard("main.start_error", lang))


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
        await _send_start_welcome(message, user, lang)
    except Exception as e:
        logger.exception("Error in start command: %s", e)
        log_error_code("E010", {"action": "start_command_error", "error": e.__class__.__name__})
        lang = get_user_language(getattr(message.from_user, "language_code", None))
        await message.answer(build_user_error_message("main.start_error", lang), reply_markup=build_error_keyboard("main.start_error", lang))


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
