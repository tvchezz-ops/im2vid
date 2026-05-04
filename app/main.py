"""Главное приложение."""
import asyncio
import signal
from typing import Optional, Set

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot.middlewares import DbSessionMiddleware
from app.bot.routers import (
    errors_router,
    generations_router,
    profile_router,
    shop_router,
    start_router,
)
from app.config import settings
from app.db import Base, db_manager
from app.services.telegram_files import (
    MEDIA_BIND_HOST,
    MEDIA_BIND_PORT,
    cleanup_old_media_files,
    create_media_app,
    ensure_public_base_url,
)
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
        self.dp.include_router(generations_router)
        self.dp.include_router(shop_router)
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

    async def start_media_server(self):
        """Поднять локальный static endpoint для media-файлов."""
        public_base_url = ensure_public_base_url()
        cleanup_old_media_files()
        app = create_media_app()
        self.media_runner = web.AppRunner(app)
        await self.media_runner.setup()
        site = web.TCPSite(self.media_runner, host=MEDIA_BIND_HOST, port=MEDIA_BIND_PORT)
        await site.start()
        logger.info(
            "Media server started on %s:%s with public base %s",
            MEDIA_BIND_HOST,
            MEDIA_BIND_PORT,
            public_base_url,
        )

    async def start(self):
        """Запустить бота."""
        logger.info("Starting Telegram bot...")
        
        try:
            # Создание таблиц
            await self.create_tables()
            
            # Установка команд
            await self.setup_commands()

            # Публикация локальных media-файлов
            await self.start_media_server()
            
            # Подключение обработчиков
            await self.setup_handlers()
            
            # Запуск polling
            logger.info("Bot is running...")
            await self.dp.start_polling(self.bot)
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
