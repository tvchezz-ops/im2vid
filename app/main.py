"""Главное приложение."""
import asyncio
import os
import signal
from typing import Optional, Set

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot.middlewares import DbSessionMiddleware
from app.bot.routers import (
    errors_router,
    generations_router,
    payments_router,
    profile_router,
    start_router,
)
from app.config import settings
from app.db import Base, db_manager
from app.services.telegram_files import (
    MEDIA_BIND_HOST,
    cleanup_old_temp_media_files,
    create_media_app,
    ensure_public_base_url,
)
from app.services.download_links import DownloadLinkService
from app.utils import logger


class TelegramBot:
    """Главный класс Telegram бота."""

    def __init__(self):
        """Инициализация бота."""
        self.bot = Bot(token=settings.bot_token)
        self.storage = MemoryStorage()
        self.dp = Dispatcher(storage=self.storage)
        self.running_tasks: Set[asyncio.Task] = set()
        self.media_runner: Optional[web.AppRunner] = None

    async def log_bot_identity(self):
        """Log the Telegram bot identity so duplicate deployments are easier to spot."""
        me = await self.bot.get_me()
        logger.info(
            {
                "action": "telegram_bot_identity",
                "bot_id": me.id,
                "bot_username": me.username,
                "instance_name": settings.instance_name.strip() or None,
            }
        )

    async def setup_commands(self):
        """Установить команды бота."""
        commands = [
            BotCommand(command="start", description="Начать работу с ботом"),
            BotCommand(command="help", description="Справка"),
            BotCommand(command="profile", description="Мой профиль"),
        ]
        await self.bot.set_my_commands(commands)
        logger.info("Bot commands set")

    async def setup_handlers(self):
        """Подключить обработчики."""
        self.dp.update.middleware(DbSessionMiddleware())
        self.dp.include_router(errors_router)
        self.dp.include_router(start_router)
        self.dp.include_router(profile_router)
        self.dp.include_router(payments_router)
        self.dp.include_router(generations_router)
        logger.info("Handlers installed")

    async def create_tables(self):
        """Создать таблицы в БД."""
        try:
            async with db_manager.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created")
        except Exception as e:
            logger.exception("Error creating database tables: %s", e)
            raise

    @staticmethod
    def should_create_tables() -> bool:
        """Разрешить create_all только в локальном dev-режиме."""
        return os.getenv("ENV", "").strip().lower() == "dev"

    async def start_media_server(self):
        """Поднять локальный static endpoint для временных media-файлов."""
        port = int(os.getenv("PORT", 8080))
        ensure_public_base_url()
        cleanup_old_temp_media_files()
        deleted_links = await DownloadLinkService().delete_expired_download_links()
        if deleted_links:
            logger.info("Deleted expired download links: %s", deleted_links)
        app = create_media_app(self.bot)
        self.media_runner = web.AppRunner(app)
        await self.media_runner.setup()
        site = web.TCPSite(self.media_runner, host=MEDIA_BIND_HOST, port=port)
        await site.start()
        logger.info("Media server started on 0.0.0.0:%s", port)

    async def start(self):
        """Запустить бота."""
        logger.info({"action": "telegram_bot_starting", "instance_name": settings.instance_name.strip() or None})
        
        try:
            # Создание таблиц только в локальном dev-режиме.
            if self.should_create_tables():
                await self.create_tables()
            else:
                logger.info("Skipping automatic database schema creation; use Alembic migrations")

            await self.log_bot_identity()
            
            # Установка команд
            await self.setup_commands()

            # Публикация локальных media-файлов
            await self.start_media_server()
            
            # Подключение обработчиков
            await self.setup_handlers()
            
            # Запуск polling
            logger.info("Bot is running...")
            await self.dp.start_polling(self.bot)
        except TelegramConflictError:
            logger.error(
                {
                    "action": "telegram_conflict_error",
                    "instance_name": settings.instance_name.strip() or None,
                    "message": "Another getUpdates consumer is already running for this BOT_TOKEN. Stop the duplicate local process or Railway service, then clear webhook with deleteWebhook?drop_pending_updates=true.",
                }
            )
            raise
        except Exception as e:
            logger.exception("Error starting bot: %s", e)
            raise

    async def stop(self):
        """Остановить бота."""
        logger.info("Stopping bot...")
        
        # Отмена всех задач
        for task in self.running_tasks:
            if not task.done():
                task.cancel()
        
        # Закрыть сессию бота
        await self.bot.session.close()

        if self.media_runner is not None:
            await self.media_runner.cleanup()
            self.media_runner = None
        
        # Закрыть соединения БД
        await db_manager.dispose()
        
        logger.info("Bot stopped")


async def main():
    """Главная функция."""
    bot = TelegramBot()
    
    # Обработка сигналов для graceful shutdown
    loop = asyncio.get_event_loop()
    
    def signal_handler(sig):
        logger.info(f"Received signal {sig}, shutting down...")
        loop.create_task(bot.stop())
    
    # Регистрируем обработчики сигналов
    loop.add_signal_handler(signal.SIGTERM, signal_handler, signal.SIGTERM)
    loop.add_signal_handler(signal.SIGINT, signal_handler, signal.SIGINT)
    
    try:
        await bot.start()
    except asyncio.CancelledError:
        logger.info("Startup cancelled")
    except Exception as e:
        logger.critical(f"Critical error: {e}")
        raise
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user")
