"""Модели БД."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseModel
from app.utils.referrals import generate_referral_code, generate_start_payload


class GenerationRequestStatus(str, enum.Enum):
    """Статусы запроса генерации."""

    CREATED = "created"
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    DELIVERY_FAILED = "delivery_failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    FAILED = "failed"


class PaymentProvider(str, enum.Enum):
    """Провайдеры платежей."""

    TELEGRAM_STARS = "telegram_stars"
    NOWPAYMENTS = "nowpayments"


class PaymentOrderStatus(str, enum.Enum):
    """Статусы платежного заказа."""

    CREATED = "created"
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ReferralEventStatus(str, enum.Enum):
    """Referral processing result status."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReferralRejectReason(str, enum.Enum):
    """Reasons why a referral payload was rejected."""

    SELF_REFERRAL = "self_referral"
    ALREADY_REFERRED = "already_referred"
    ALREADY_REGISTERED = "already_registered"
    INVALID_CODE = "invalid_code"
    REFERRER_NOT_FOUND = "referrer_not_found"


class User(BaseModel):
    """Модель пользователя."""
    
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "referred_by_user_id IS NULL OR referred_by_user_id != id",
            name="ck_users_not_self_referred",
        ),
    )
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    language_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_premium: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    photo_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    balance: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    referral_code: Mapped[Optional[str]] = mapped_column(
        String(10),
        unique=True,
        index=True,
        nullable=True,
        default=generate_referral_code,
    )
    start_payload: Mapped[Optional[str]] = mapped_column(
        String(24),
        unique=True,
        index=True,
        nullable=True,
        default=generate_start_payload,
    )
    referred_by_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    referred_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    send_results_as_files: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    total_generations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    successful_generations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_generations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class ReferralEvent(Base):
    """Immutable audit log for accepted and rejected referral attempts."""

    __tablename__ = "referral_events"
    __table_args__ = (
        # The database enforces one accepted referral forever, which keeps retries idempotent.
        Index(
            "uq_referral_events_accepted_referred_user_id",
            "referred_user_id",
            unique=True,
            sqlite_where=text("status = 'accepted'"),
            postgresql_where=text("status = 'accepted'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    referrer_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True, index=True)
    referred_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    referral_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class CreditTransaction(Base):
    """Ledger record for credit balance changes."""

    __tablename__ = "credit_transactions"
    __table_args__ = (
        Index(
            "uq_credit_transactions_referral_bonus_event_user",
            "user_id",
            "referral_event_id",
            unique=True,
            sqlite_where=text("type = 'referral_bonus'"),
            postgresql_where=text("type = 'referral_bonus'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    referral_event_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, ForeignKey("referral_events.id"), nullable=True, index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class GenerationRequest(BaseModel):
    """Запрос пользователя на генерацию."""
    
    __tablename__ = "generation_requests"
    
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    model_key: Mapped[str] = mapped_column(String(100), index=True)
    model_endpoint: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    input_image_file_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_image_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    aspect_ratio: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    resolution: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    size: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    output_format: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    wavespeed_prediction_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[GenerationRequestStatus] = mapped_column(
        Enum(GenerationRequestStatus, native_enum=False),
        default=GenerationRequestStatus.CREATED,
        server_default=GenerationRequestStatus.CREATED.value,
    )
    output_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    nsfw_flags: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cost: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class UserEvent(Base):
    """События пользователей и системы."""
    
    __tablename__ = "user_events"
    
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class Payment(Base):
    """Платежи пользователей."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    provider: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class PaymentOrder(BaseModel):
    """Платежный заказ для пополнения баланса."""

    __tablename__ = "payment_orders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(
        String(50),
        default=PaymentOrderStatus.CREATED.value,
        server_default=PaymentOrderStatus.CREATED.value,
        index=True,
    )
    amount: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(20))
    external_payment_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    telegram_payment_charge_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    nowpayments_payment_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    payload: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    metadata_: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata",
        JSON,
        nullable=True,
        default=dict,
        server_default="{}",
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DownloadLink(Base):
    """Короткая временная ссылка на R2-объект."""

    __tablename__ = "download_links"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    r2_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
