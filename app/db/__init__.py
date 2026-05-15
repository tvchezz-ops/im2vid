"""Инициализация пакета db."""
from importlib import import_module

from app.db.base import Base, BaseModel
from app.db.models import (
    CreditTransaction,
    DownloadLink,
    GenerationRequest,
    GenerationRequestStatus,
    Payment,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentProvider,
    ReferralEvent,
    ReferralEventStatus,
    ReferralRejectReason,
    User,
    UserEvent,
)
from app.db.repositories import (
    DownloadLinkRepository,
    EventRepository,
    GenerationRepository,
    PaymentCompletionResult,
    PaymentRepository,
    UserCreateResult,
    UserRepository,
)

__all__ = [
    "Base",
    "BaseModel",
    "CreditTransaction",
    "User",
    "GenerationRequest",
    "GenerationRequestStatus",
    "UserEvent",
    "Payment",
    "PaymentOrder",
    "PaymentOrderStatus",
    "PaymentProvider",
    "ReferralEvent",
    "ReferralEventStatus",
    "ReferralRejectReason",
    "DownloadLink",
    "UserRepository",
    "GenerationRepository",
    "EventRepository",
    "PaymentRepository",
    "PaymentCompletionResult",
    "UserCreateResult",
    "DownloadLinkRepository",
    "DatabaseManager",
    "db_manager",
    "get_session",
]


def __getattr__(name: str):
    if name in {"DatabaseManager", "db_manager", "get_session"}:
        session = import_module("app.db.session")

        return getattr(session, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
