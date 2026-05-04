"""Примеры использования сервисов и репозиториев."""

# ============================================================================
# Пример 1: Работа с репозиториями пользователей
# ============================================================================

async def user_example(session: AsyncSession):
    """Пример работы с пользователями."""
    from app.db import UserRepository
    
    user_repo = UserRepository(session)
    
    # Создать или обновить пользователя
    user = await user_repo.create_or_update(
        telegram_id=123456789,
        username="john_doe",
        first_name="John",
        last_name="Doe",
        language_code="en",
    )
    print(f"User created: {user.first_name} (balance: {user.balance})")
    
    # Получить пользователя по telegram_id
    user = await user_repo.get_by_telegram_id(123456789)
    print(f"User: {user.first_name}")
    
    # Обновить баланс
    user = await user_repo.update_balance(123456789, 50.0)
    print(f"New balance: {user.balance}")


# ============================================================================
# Пример 2: Работа с генерациями
# ============================================================================

async def generation_example(session: AsyncSession):
    """Пример работы с генерациями."""
    from app.db import GenerationRepository
    
    gen_repo = GenerationRepository(session)
    
    # Создать генерацию
    generation = await gen_repo.create(
        user_id=1,
        prompt="Create a beautiful sunset image",
        cost=5.0,
    )
    print(f"Generation created: {generation.id}")
    
    # Получить генерацию
    generation = await gen_repo.get_by_id(1)
    print(f"Status: {generation.status}")
    
    # Обновить статус
    generation = await gen_repo.update_status(
        generation_id=1,
        status="completed",
        result_url="https://example.com/image.png",
    )
    print(f"Status updated: {generation.status}")


# ============================================================================
# Пример 3: Работа с WavespeedService
# ============================================================================

async def wavespeed_example():
    """Пример работы с API Wavespeed."""
    from app.services import WavespeedService
    
    service = WavespeedService()
    
    try:
        # Запустить генерацию
        response = await service.generate(
            prompt="A beautiful garden in spring",
            model="premium",
        )
        print(f"Generation started: {response['id']}")
        
        # Получить статус
        status = await service.get_status(response['id'])
        print(f"Status: {status['status']}")
    finally:
        await service.close()


# ============================================================================
# Пример 4: Работа с BlacklistRepository
# ============================================================================

async def blacklist_example(session: AsyncSession):
    """Пример работы с черным списком файлов."""
    from app.db import FileBlacklistRepository
    
    repo = FileBlacklistRepository(session)
    
    # Добавить файл в черный список
    item = await repo.add(
        file_id="AgAC...",
        reason="Copyrighted content",
    )
    print(f"File added to blacklist: {item.file_id}")
    
    # Проверить
    is_blacklisted = await repo.is_blacklisted("AgAC...")
    print(f"Is blacklisted: {is_blacklisted}")


# ============================================================================
# Пример 5: Добавление нового роутера
# ============================================================================

# Файл: app/bot/routers/help.py

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("help"))
async def help_command(message: Message):
    """Команда помощи."""
    help_text = (
        "❓ <b>Справка</b>\n\n"
        "/start - Начать работу\n"
        "/profile - Мой профиль\n"
        "/help - Эта справка"
    )
    await message.answer(help_text, parse_mode="HTML")


# Затем добавить в app/bot/routers/__init__.py:
# from .help import router as help_router
# И в app/main.py:
# self.dp.include_router(help_router)


# ============================================================================
# Пример 6: Обработка ошибок
# ============================================================================

async def error_handling_example(message: Message, session: AsyncSession):
    """Пример обработки ошибок."""
    from app.db import UserRepository
    from app.utils import logger
    
    try:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_telegram_id(message.from_user.id)
        
        if not user:
            await message.answer("❌ Пользователь не найден")
            return
        
        if user.balance < 10:
            await message.answer("❌ Недостаточно средств")
            return
        
        # Выполнить операцию
        await message.answer("✅ Успешно!")
        
    except ValueError as e:
        logger.warning(f"Validation error: {e}")
        await message.answer("❌ Ошибка валидации")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")


# ============================================================================
# Пример 7: Асинхронная БД работа
# ============================================================================

async def database_operations():
    """Пример асинхронных операций с БД."""
    from sqlalchemy import select
    from app.db import db_manager, User
    
    async with db_manager.session_factory() as session:
        # Получить всех пользователей
        result = await session.execute(select(User))
        users = result.scalars().all()
        
        for user in users:
            print(f"User: {user.first_name} (balance: {user.balance})")
        
        # Коммит изменений
        await session.commit()
