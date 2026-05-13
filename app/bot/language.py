"""Helpers for resolving a user's UI language from bot events."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import UserRepository
from app.i18n import DEFAULT_LANGUAGE, get_user_language


def get_event_actor(message_or_callback: Any) -> Any | None:
    return getattr(message_or_callback, "from_user", None)


async def get_event_lang(message_or_callback: Any, session: AsyncSession | None = None) -> str:
    actor = get_event_actor(message_or_callback)
    actor_language = get_user_language(getattr(actor, "language_code", None)) if actor is not None else DEFAULT_LANGUAGE
    actor_id = getattr(actor, "id", None)
    if session is not None and actor_id is not None:
        user = await UserRepository(session).get_by_telegram_id(int(actor_id))
        if user is not None and getattr(user, "language_code", None):
            return get_user_language(user.language_code)
    return actor_language