"""Сессии БД."""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.utils import logger


class DatabaseManager:
    """Менеджер для управления БД."""

    def __init__(self, database_url: str):
        """Инициализация."""
        self.engine = create_async_engine(
            database_url,
            echo=False,  # Отключаем echo в production
            future=True,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def dispose(self):
        """Закрыть все соединения."""
        await self.engine.dispose()

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Получить сессию БД."""
        async with self.session_factory() as session:
            yield session


# Глобальный менеджер
db_manager = DatabaseManager(settings.database_url)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Зависимость для получения сессии БД."""
    async with db_manager.session_factory() as session:
        yield session
