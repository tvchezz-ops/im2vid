"""Minimal standalone Telegram Stars wallet bot."""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import uuid
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import PaymentOrder, PaymentOrderStatus
from app.db.repositories import PaymentRepository


logger = logging.getLogger(__name__)
router = Router()
WALLET_PAYMENT_PROVIDER = "telegram_stars_wallet_bot"


class WalletSettings(BaseSettings):
    """Environment for the standalone wallet bot."""

    wallet_bot_token: str = Field(..., alias="WALLET_BOT_TOKEN")
    main_bot_username: str = Field(..., alias="MAIN_BOT_USERNAME")
    database_url: str = Field(..., alias="DATABASE_URL")
    wallet_allowed_amounts: str = Field(
        default="100,300,500,1000,3000,5000",
        alias="WALLET_ALLOWED_AMOUNTS",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @property
    def normalized_main_bot_username(self) -> str:
        return self.main_bot_username.strip().lstrip("@")

    @property
    def allowed_amounts(self) -> tuple[int, ...]:
        return tuple(
            int(raw_amount.strip())
            for raw_amount in self.wallet_allowed_amounts.split(",")
            if raw_amount.strip()
        )


def parse_amount_from_start_payload(payload: Optional[str]) -> Optional[int]:
    """Parse supported wallet deep-link payloads: 100credits or pay_100."""
    if not payload:
        return None

    normalized_payload = payload.strip()
    match = re.fullmatch(r"(?P<amount>\d+)credits", normalized_payload)
    if match:
        return int(match.group("amount"))

    match = re.fullmatch(r"pay_(?P<amount>\d+)", normalized_payload)
    if match:
        return int(match.group("amount"))

    return None


def extract_start_payload(message: Message, command: CommandObject | None = None) -> str:
    """Extract the raw payload from `/start payload` message text."""
    text = (getattr(message, "text", None) or "").strip()
    if text == "/start" or text.startswith("/start "):
        return text.removeprefix("/start").strip()
    if command is not None:
        return (command.args or "").strip()
    return ""


def build_invoice_payload(user_id: int, amount: int) -> str:
    return f"wallet:{user_id}:{amount}:{uuid.uuid4().hex}"


def parse_amount_from_invoice_payload(payload: str) -> Optional[int]:
    match = re.fullmatch(r"wallet:(?P<user_id>\d+):(?P<amount>\d+):[a-f0-9]{32}", payload.strip())
    if match is None:
        return None
    return int(match.group("amount"))


def build_return_keyboard(settings: WalletSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Return to generation bot",
                    url=f"https://t.me/{settings.normalized_main_bot_username}?start=payment_success",
                )
            ]
        ]
    )


def detect_database_backend(database_url: str) -> str:
    if database_url.startswith("sqlite"):
        return "sqlite"
    if database_url.startswith("postgres"):
        return "postgresql"
    return database_url.split(":", 1)[0]


def normalize_database_url(database_url: str) -> str:
    normalized_url = database_url.strip()
    if normalized_url.startswith("postgres://"):
        return normalized_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if normalized_url.startswith("postgresql://"):
        return normalized_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return normalized_url


def create_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(normalize_database_url(database_url), echo=False, future=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def create_wallet_payment_order(
    session: AsyncSession,
    *,
    user_id: int,
    amount: int,
) -> PaymentOrder:
    payload = build_invoice_payload(user_id, amount)
    return await PaymentRepository(session).create_payment_order(
        user_id=user_id,
        provider=WALLET_PAYMENT_PROVIDER,
        amount=amount,
        credits=amount,
        currency="XTR",
        payload=payload,
        metadata={},
    )


async def complete_wallet_payment(
    session: AsyncSession,
    *,
    payload: str,
    total_amount: int,
    currency: str,
    telegram_payment_charge_id: str,
) -> Optional[PaymentOrder]:
    if currency != "XTR":
        return None
    completion = await PaymentRepository(session).complete_payment_and_credit_user(
        payload=payload,
        telegram_payment_charge_id=telegram_payment_charge_id,
        total_amount=total_amount,
    )
    order = completion.order
    if order is None or order.currency != "XTR":
        return None
    return order


@router.message(Command("start"))
async def start_command(
    message: Message,
    command: CommandObject | None,
    settings: WalletSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    start_payload = extract_start_payload(message, command)

    if not start_payload:
        logger.info({"action": "ignored_start", "user_id": message.from_user.id if message.from_user else None})
        return

    amount = parse_amount_from_start_payload(start_payload)
    if amount not in settings.allowed_amounts:
        logger.info(
            {
                "action": "invalid_payment_link",
                "user_id": message.from_user.id if message.from_user else None,
            }
        )
        await message.answer("Invalid payment link.")
        return

    user_id = message.from_user.id if message.from_user else 0
    async with session_factory() as session:
        order = await create_wallet_payment_order(session, user_id=user_id, amount=amount)

    await message.answer_invoice(
        title=f"{amount} credits",
        description=f"Top up {amount} credits",
        payload=order.payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} credits", amount=amount)],
    )
    logger.info(
        {
            "action": "wallet_invoice_sent",
            "order_id": str(order.id),
            "user_id": user_id,
            "amount": amount,
        }
    )


@router.pre_checkout_query()
async def process_pre_checkout_query(
    pre_checkout_query: PreCheckoutQuery,
    settings: WalletSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        order = await PaymentRepository(session).get_payment_order_by_payload(pre_checkout_query.invoice_payload)
        if (
            order is not None
            and order.status != PaymentOrderStatus.PAID.value
            and pre_checkout_query.total_amount == order.amount
            and order.currency == "XTR"
            and order.amount in settings.allowed_amounts
            and pre_checkout_query.currency == "XTR"
        ):
            await pre_checkout_query.answer(ok=True)
            return

    logger.info(
        {
            "action": "wallet_pre_checkout_rejected",
            "user_id": pre_checkout_query.from_user.id,
            "amount": pre_checkout_query.total_amount,
        }
    )
    await pre_checkout_query.answer(ok=False, error_message="Payment order not found")


@router.message(F.successful_payment)
async def process_successful_payment(
    message: Message,
    settings: WalletSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    successful_payment = message.successful_payment
    if successful_payment is None:
        return

    async with session_factory() as session:
        order = await complete_wallet_payment(
            session,
            payload=successful_payment.invoice_payload,
            total_amount=successful_payment.total_amount,
            currency=successful_payment.currency,
            telegram_payment_charge_id=successful_payment.telegram_payment_charge_id,
        )
    if order is None:
        logger.info(
            {
                "action": "wallet_payment_rejected",
                "user_id": message.from_user.id if message.from_user else None,
                "amount": successful_payment.total_amount,
            }
        )
        return

    logger.info(
        {
            "action": "wallet_payment_successful",
            "order_id": str(order.id),
            "user_id": message.from_user.id if message.from_user else None,
            "amount": successful_payment.total_amount,
        }
    )
    await message.answer(
        f"✅ Payment received.\n{order.credits} credits added to your balance.",
        reply_markup=build_return_keyboard(settings),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    settings = WalletSettings()
    logger.info({"action": "wallet_bot_starting", "database_backend": detect_database_backend(settings.database_url)})

    bot = Bot(token=settings.wallet_bot_token)
    session_factory = create_session_factory(settings.database_url)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot, settings=settings, session_factory=session_factory)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)