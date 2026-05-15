"""Сессии БД."""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.database_url import (
    database_connection_mode,
    database_url_host,
    is_production_env,
    is_public_database_endpoint,
    normalize_database_url,
)
from app.utils import logger


def detect_database_backend(database_url: str) -> str:
    """Определить тип БД без вывода полного URL."""
    if database_url.startswith("sqlite"):
        return "sqlite"
    if database_url.startswith("postgresql+asyncpg"):
        return "postgresql"
    return database_url.split(":", 1)[0]


class DatabaseManager:
    """Менеджер для управления БД."""

    def __init__(self, database_url: str, *, env: str = "", strict_private_network: bool = False):
        """Инициализация."""
        normalized_database_url = normalize_database_url(database_url)
        connection_mode = database_connection_mode(normalized_database_url)
        connection_host = database_url_host(normalized_database_url)
        logger.info(
            {
                "action": "database_connection_mode",
                "mode": connection_mode,
                "host": connection_host,
            }
        )
        if is_production_env(env) and is_public_database_endpoint(normalized_database_url):
            logger.warning(
                {
                    "action": "database_public_endpoint_warning",
                    "mode": connection_mode,
                    "host": connection_host,
                    "reason": "public_database_endpoint_in_production",
                }
            )
            if strict_private_network:
                raise RuntimeError("STRICT_PRIVATE_NETWORK=true rejects public Railway database endpoints in production")
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
db_manager = DatabaseManager(
    settings.database_url,
    env=settings.env,
    strict_private_network=settings.strict_private_network,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Зависимость для получения сессии БД."""
    async with db_manager.session_factory() as session:
        yield session
