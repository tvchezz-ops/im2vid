"""Модели БД."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BaseModel


class GenerationRequestStatus(str, enum.Enum):
    """Статусы запроса генерации."""

    CREATED = "created"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class User(BaseModel):
    """Модель пользователя."""
    
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    language_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    is_premium: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    photo_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    balance: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    total_generations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    successful_generations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_generations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class GenerationRequest(BaseModel):
    """Запрос пользователя на генерацию."""
    
    __tablename__ = "generation_requests"
    
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    model_key: Mapped[str] = mapped_column(String(100), index=True)
    model_endpoint: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[str] = mapped_column(Text)
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
