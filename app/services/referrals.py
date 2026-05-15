"""Referral application business logic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import CreditTransaction, ReferralEvent, ReferralEventStatus, ReferralRejectReason, User
from app.utils import logger
from app.utils.referrals import mask_start_payload

ReferralApplyStatus = Literal["none", "accepted", "rejected"]


@dataclass(frozen=True)
class ReferralApplyResult:
    """Result of applying a referral start payload."""

    status: ReferralApplyStatus
    reason: str | None = None
    referrer_user_id: int | None = None
    referred_user_id: int | None = None
    referral_code: str | None = None
    referrer_bonus_credits: int = 0
    referred_bonus_credits: int = 0


class ReferralService:
    """Apply referral rules for a newly created Telegram user."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def apply_referral(
        self,
        new_user: User,
        referral_code: str | None,
        *,
        created: bool | None = None,
        referrer: User | None = None,
    ) -> ReferralApplyResult:
        """Apply a referral code exactly once during a user's first start."""
        code = (referral_code or "").strip()
        logger.info(
            {
                "action": "referral_apply_started",
                "user_id": new_user.id,
                "referral_code_present": bool(code),
                "payload_prefix": mask_start_payload(code),
            }
        )

        if not code:
            return ReferralApplyResult(status="none", referred_user_id=new_user.id)

        referrer = referrer or await self._get_referrer(code)
        if referrer is None:
            return await self._reject(new_user, code, ReferralRejectReason.INVALID_CODE)

        if referrer.id == new_user.id:
            return await self._reject(new_user, code, ReferralRejectReason.SELF_REFERRAL, referrer.id)

        if new_user.referred_by_user_id is not None:
            return await self._reject(new_user, code, ReferralRejectReason.ALREADY_REFERRED, referrer.id)

        # The creation flag is intentionally transient: the DB records when a user was created,
        # while the repository tells this /start call whether that creation happened just now.
        if not self._is_newly_created_user(new_user, created):
            return await self._reject(new_user, code, ReferralRejectReason.ALREADY_REGISTERED, referrer.id)

        return await self._accept(new_user, referrer, code)

    async def _get_referrer(self, code: str) -> User | None:
        result = await self.session.execute(select(User).where(User.referral_code == code))
        return result.scalars().first()

    @staticmethod
    def _is_newly_created_user(user: User, created: bool | None) -> bool:
        if created is not None:
            return created
        return bool(getattr(user, "newly_created_user", False))

    async def _reject(
        self,
        user: User,
        code: str,
        reason: ReferralRejectReason,
        referrer_user_id: int | None = None,
    ) -> ReferralApplyResult:
        event = ReferralEvent(
            referrer_user_id=referrer_user_id,
            referred_user_id=user.id,
            referral_code=code,
            status=ReferralEventStatus.REJECTED.value,
            reject_reason=reason.value,
        )
        self.session.add(event)
        await self.session.commit()
        logger.info(
            {
                "action": "referral_rejected",
                "user_id": user.id,
                "referrer_user_id": referrer_user_id,
                "payload_prefix": mask_start_payload(code),
                "reason": reason.value,
            }
        )
        return ReferralApplyResult(
            status="rejected",
            reason=reason.value,
            referrer_user_id=referrer_user_id,
            referred_user_id=user.id,
            referral_code=code,
        )

    async def _accept(self, user: User, referrer: User, code: str) -> ReferralApplyResult:
        referrer_bonus = max(0, int(settings.referral_referrer_bonus_credits))
        referred_bonus = max(0, int(settings.referral_referred_bonus_credits))
        referred_at = datetime.now(timezone.utc)

        try:
            update_result = await self.session.execute(
                update(User)
                .where(User.id == user.id, User.referred_by_user_id.is_(None))
                .values(referred_by_user_id=referrer.id, referred_at=referred_at)
            )
            if update_result.rowcount == 0:
                await self.session.rollback()
                return await self._reject(user, code, ReferralRejectReason.ALREADY_REFERRED, referrer.id)

            event = ReferralEvent(
                referrer_user_id=referrer.id,
                referred_user_id=user.id,
                referral_code=code,
                status=ReferralEventStatus.ACCEPTED.value,
            )
            self.session.add(event)
            await self.session.flush()
            if referrer_bonus > 0:
                await self._grant_referral_bonus(
                    user_id=referrer.id,
                    amount=referrer_bonus,
                    referrer_user_id=referrer.id,
                    referred_user_id=user.id,
                    referral_event_id=event.id,
                )
            if referred_bonus > 0:
                await self._grant_referral_bonus(
                    user_id=user.id,
                    amount=referred_bonus,
                    referrer_user_id=referrer.id,
                    referred_user_id=user.id,
                    referral_event_id=event.id,
                )

            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            return await self._reject(user, code, ReferralRejectReason.ALREADY_REFERRED, referrer.id)

        await self.session.refresh(user)
        logger.info(
            {
                "action": "referral_accepted",
                "user_id": user.id,
                "referrer_user_id": referrer.id,
                "payload_prefix": mask_start_payload(code),
            }
        )
        if referrer_bonus > 0:
            self._log_bonus_granted(
                referrer_user_id=referrer.id,
                referred_user_id=user.id,
                credits=referrer_bonus,
                referral_event_id=event.id,
            )
        if referred_bonus > 0:
            self._log_bonus_granted(
                referrer_user_id=referrer.id,
                referred_user_id=user.id,
                credits=referred_bonus,
                referral_event_id=event.id,
                credited_user_id=user.id,
            )

        return ReferralApplyResult(
            status="accepted",
            referrer_user_id=referrer.id,
            referred_user_id=user.id,
            referral_code=code,
            referrer_bonus_credits=referrer_bonus,
            referred_bonus_credits=referred_bonus,
        )

    async def _grant_referral_bonus(
        self,
        *,
        user_id: int,
        amount: int,
        referrer_user_id: int,
        referred_user_id: int,
        referral_event_id: UUID,
    ) -> bool:
        existing_transaction = await self.session.scalar(
            select(CreditTransaction.id).where(
                CreditTransaction.type == "referral_bonus",
                CreditTransaction.user_id == user_id,
                CreditTransaction.referral_event_id == referral_event_id,
            )
        )
        if existing_transaction is not None:
            return False

        await self.session.execute(update(User).where(User.id == user_id).values(balance=User.balance + amount))
        self._add_bonus_transaction(
            user_id=user_id,
            amount=amount,
            referrer_user_id=referrer_user_id,
            referred_user_id=referred_user_id,
            referral_event_id=referral_event_id,
        )
        return True

    @staticmethod
    def _log_bonus_granted(
        *,
        referrer_user_id: int,
        referred_user_id: int,
        credits: int,
        referral_event_id: UUID,
        credited_user_id: int | None = None,
    ) -> None:
        logger.info(
            {
                "action": "referral_bonus_granted",
                "referrer_user_id": referrer_user_id,
                "referred_user_id": referred_user_id,
                "credits": credits,
                "referral_event_id": str(referral_event_id),
                "credited_user_id": credited_user_id or referrer_user_id,
            }
        )

    def _add_bonus_transaction(
        self,
        *,
        user_id: int,
        amount: int,
        referrer_user_id: int,
        referred_user_id: int,
        referral_event_id,
    ) -> None:
        self.session.add(
            CreditTransaction(
                type="referral_bonus",
                user_id=user_id,
                amount=amount,
                referral_event_id=referral_event_id,
                metadata_={
                    "referred_user_id": referred_user_id,
                    "referrer_user_id": referrer_user_id,
                    "referral_event_id": str(referral_event_id),
                },
            )
        )


async def apply_referral(
    session: AsyncSession,
    new_user: User,
    referral_code: str | None,
    *,
    created: bool | None = None,
    referrer: User | None = None,
) -> ReferralApplyResult:
    """Convenience wrapper matching the /start integration signature."""
    return await ReferralService(session).apply_referral(new_user, referral_code, created=created, referrer=referrer)
