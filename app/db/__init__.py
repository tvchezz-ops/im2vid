"""Инициализация пакета db."""
from importlib import import_module

from app.db.base import Base, BaseModel
from app.db.models import (
    DownloadLink,
    GenerationRequest,
    GenerationRequestStatus,
    Payment,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentProvider,
    User,
    UserEvent,
)
from app.db.repositories import (
    DownloadLinkRepository,
    EventRepository,
    GenerationRepository,
    PaymentCompletionResult,
    PaymentRepository,
    UserRepository,
)

__all__ = [
    "Base",
    "BaseModel",
    "User",
    "GenerationRequest",
    "GenerationRequestStatus",
    "UserEvent",
    "Payment",
    "PaymentOrder",
    "PaymentOrderStatus",
    "PaymentProvider",
    "DownloadLink",
    "UserRepository",
    "GenerationRepository",
    "EventRepository",
    "PaymentRepository",
    "PaymentCompletionResult",
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
