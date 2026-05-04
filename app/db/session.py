"""Сессии БД."""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.utils import logger


def normalize_database_url(database_url: str) -> str:
    """Нормализовать DATABASE_URL для async SQLAlchemy engine."""
    normalized_url = database_url.strip()
    if normalized_url.startswith("postgres://"):
        return normalized_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if normalized_url.startswith("postgresql://"):
        return normalized_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return normalized_url


def detect_database_backend(database_url: str) -> str:
    """Определить тип БД без вывода полного URL."""
    if database_url.startswith("sqlite"):
        return "sqlite"
    if database_url.startswith("postgresql+asyncpg"):
        return "postgresql"
    return database_url.split(":", 1)[0]


class DatabaseManager:
    """Менеджер для управления БД."""

    def __init__(self, database_url: str):
        """Инициализация."""
        normalized_database_url = normalize_database_url(database_url)
        logger.info("Using database backend: %s", detect_database_backend(normalized_database_url))
        self.engine = create_async_engine(
            normalized_database_url,
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
