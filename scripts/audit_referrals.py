"""Audit referral data consistency."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import ReferralEvent, ReferralEventStatus, User
from app.db.session import normalize_database_url
from app.utils import logger


@dataclass(frozen=True)
class ReferralAuditIssue:
    check: str
    message: str
    count: int


def _issue(check: str, message: str, count: int) -> ReferralAuditIssue | None:
    if count <= 0:
        return None
    return ReferralAuditIssue(check=check, message=message, count=count)


async def audit_referrals(session: AsyncSession) -> list[ReferralAuditIssue]:
    """Return referral consistency issues found in the current database."""
    issues: list[ReferralAuditIssue] = []

    self_event_count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ReferralEvent)
        .where(
            ReferralEvent.status == ReferralEventStatus.ACCEPTED.value,
            ReferralEvent.referrer_user_id == ReferralEvent.referred_user_id,
        )
    )
    issues.extend(
        issue
        for issue in [
            _issue(
                "accepted_self_referrals",
                "accepted referral events must not have the same referrer and referred user",
                int(self_event_count or 0),
            )
        ]
        if issue is not None
    )

    self_user_count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(User)
        .where(User.referred_by_user_id == User.id)
    )
    issues.extend(
        issue
        for issue in [
            _issue(
                "users_self_referred",
                "users.referred_by_user_id must not equal users.id",
                int(self_user_count or 0),
            )
        ]
        if issue is not None
    )

    duplicate_accepted_subquery = (
        sa.select(ReferralEvent.referred_user_id)
        .where(ReferralEvent.status == ReferralEventStatus.ACCEPTED.value)
        .group_by(ReferralEvent.referred_user_id)
        .having(sa.func.count() > 1)
        .subquery()
    )
    duplicate_accepted_count = await session.scalar(
        sa.select(sa.func.count()).select_from(duplicate_accepted_subquery)
    )
    issues.extend(
        issue
        for issue in [
            _issue(
                "duplicate_accepted_referrals",
                "only one accepted referral event is allowed per referred user",
                int(duplicate_accepted_count or 0),
            )
        ]
        if issue is not None
    )

    accepted_mismatch_count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ReferralEvent)
        .outerjoin(User, User.id == ReferralEvent.referred_user_id)
        .where(
            ReferralEvent.status == ReferralEventStatus.ACCEPTED.value,
            sa.or_(
                User.id.is_(None),
                User.referred_by_user_id.is_distinct_from(ReferralEvent.referrer_user_id),
            ),
        )
    )
    issues.extend(
        issue
        for issue in [
            _issue(
                "accepted_event_user_mismatch",
                "accepted referral events must match users.referred_by_user_id",
                int(accepted_mismatch_count or 0),
            )
        ]
        if issue is not None
    )

    duplicate_codes_subquery = (
        sa.select(User.referral_code)
        .where(User.referral_code.is_not(None))
        .group_by(User.referral_code)
        .having(sa.func.count() > 1)
        .subquery()
    )
    duplicate_code_count = await session.scalar(sa.select(sa.func.count()).select_from(duplicate_codes_subquery))
    issues.extend(
        issue
        for issue in [
            _issue(
                "duplicate_referral_codes",
                "referral codes must be unique across users",
                int(duplicate_code_count or 0),
            )
        ]
        if issue is not None
    )

    return issues


def log_audit_result(issues: Sequence[ReferralAuditIssue]) -> None:
    if not issues:
        logger.info({"action": "referral_audit_passed"})
        return

    for issue in issues:
        logger.error(
            {
                "action": "referral_audit_failed",
                "check": issue.check,
                "count": issue.count,
                "reason": issue.message,
            }
        )


async def _run() -> int:
    engine = create_async_engine(normalize_database_url(settings.database_url))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            issues = await audit_referrals(session)
            log_audit_result(issues)
            if not issues:
                print("Referral audit passed")
                return 0

            print("Referral audit failed")
            for issue in issues:
                print(f"- {issue.check}: {issue.count} ({issue.message})")
            return 1
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
