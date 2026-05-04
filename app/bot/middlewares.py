"""Middleware для aiogram."""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import db_manager


class DbSessionMiddleware(BaseMiddleware):
    """Добавляет SQLAlchemy AsyncSession в data для хендлеров."""

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        async with db_manager.session_factory() as session:
            data["session"] = session
            return await handler(event, data)