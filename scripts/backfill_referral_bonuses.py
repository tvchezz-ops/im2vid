"""Backfill missing referral bonus credit transactions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys
from uuid import UUID

import sqlalchemy as sa
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.db.models import CreditTransaction, ReferralEvent, ReferralEventStatus, User
from app.utils import logger


class ReferralBackfillSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(default="sqlite+aiosqlite:///./bot.db", alias="DATABASE_URL")
    referral_referrer_bonus_credits: int = Field(default=5, alias="REFERRAL_REFERRER_BONUS_CREDITS")


backfill_settings = ReferralBackfillSettings()


@dataclass(frozen=True)
class ReferralBonusBackfillSummary:
    processed: int = 0
    credited: int = 0
    skipped_existing: int = 0
    total_credits_added: int = 0


async def _has_referrer_bonus_transaction(
    session: AsyncSession,
    *,
    referrer_user_id: int,
    referral_event_id: UUID,
) -> bool:
    transaction_id = await session.scalar(
        sa.select(CreditTransaction.id).where(
            CreditTransaction.type == "referral_bonus",
            CreditTransaction.user_id == referrer_user_id,
            CreditTransaction.referral_event_id == referral_event_id,
        )
    )
    return transaction_id is not None


def normalize_database_url(database_url: str) -> str:
    normalized_url = database_url.strip()
    if normalized_url.startswith("postgres://"):
        return normalized_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if normalized_url.startswith("postgresql://"):
        return normalized_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return normalized_url


async def backfill_referral_bonuses(
    session: AsyncSession,
    *,
    bonus_credits: int | None = None,
) -> ReferralBonusBackfillSummary:
    """Credit referrers for accepted referrals that are missing bonus transactions."""
    bonus_credits = max(0, int(backfill_settings.referral_referrer_bonus_credits if bonus_credits is None else bonus_credits))
    result = await session.execute(
        sa.select(ReferralEvent)
        .where(
            ReferralEvent.status == ReferralEventStatus.ACCEPTED.value,
            ReferralEvent.referrer_user_id.is_not(None),
        )
        .order_by(ReferralEvent.created_at, ReferralEvent.id)
    )
    events = result.scalars().all()

    processed = 0
    credited = 0
    skipped_existing = 0
    total_credits_added = 0

    for event in events:
        processed += 1
        if await _has_referrer_bonus_transaction(
            session,
            referrer_user_id=event.referrer_user_id,
            referral_event_id=event.id,
        ):
            skipped_existing += 1
            continue

        if bonus_credits <= 0:
            continue

        await session.execute(
            sa.update(User)
            .where(User.id == event.referrer_user_id)
            .values(balance=User.balance + bonus_credits)
        )
        session.add(
            CreditTransaction(
                type="referral_bonus",
                user_id=event.referrer_user_id,
                amount=bonus_credits,
                referral_event_id=event.id,
                metadata_={
                    "referred_user_id": event.referred_user_id,
                    "referrer_user_id": event.referrer_user_id,
                    "referral_event_id": str(event.id),
                },
            )
        )
        credited += 1
        total_credits_added += bonus_credits
        logger.info(
            {
                "action": "referral_bonus_granted",
                "referrer_user_id": event.referrer_user_id,
                "referred_user_id": event.referred_user_id,
                "credits": bonus_credits,
                "referral_event_id": str(event.id),
            }
        )

    await session.commit()
    return ReferralBonusBackfillSummary(
        processed=processed,
        credited=credited,
        skipped_existing=skipped_existing,
        total_credits_added=total_credits_added,
    )


async def _run() -> int:
    engine = create_async_engine(normalize_database_url(backfill_settings.database_url))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            summary = await backfill_referral_bonuses(session)
            print(f"processed={summary.processed}")
            print(f"credited={summary.credited}")
            print(f"skipped_existing={summary.skipped_existing}")
            print(f"total_credits_added={summary.total_credits_added}")
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
