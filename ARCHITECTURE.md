"""ARCHITECTURE.md - Архитектура проекта"""

# Архитектура Telegram Bot на aiogram 3

## Обзор архитектуры

Проект построен на основе чистой архитектуры (Clean Architecture) с четкой разделением ответственности между слоями.

```
┌─────────────────────────────────────────────┐
│         Telegram Bot (aiogram)              │
├─────────────────────────────────────────────┤
│  Routers Layer (обработчики команд)         │
│  • /start, /profile, /help                  │
│  • FSM диалоги                              │
├─────────────────────────────────────────────┤
│  Services Layer (бизнес-логика)            │
│  • WavespeedService (API интеграция)        │
│  • GenerationService (управление контентом) │
│  • TelegramFilesService (работа с файлами)  │
├─────────────────────────────────────────────┤
│  Repository Layer (доступ к данным)        │
│  • UserRepository                           │
│  • GenerationRepository                     │
│  • FileBlacklistRepository                  │
├─────────────────────────────────────────────┤
│  Data Layer (БД)                            │
│  • SQLAlchemy ORM                           │
│  • Async drivers (aiosqlite, asyncpg)       │
│  • Alembic migrations                       │
└─────────────────────────────────────────────┘
```

## Описание компонентов

### 1. Routers Layer (`app/bot/routers/`)

Обработчики команд и событий Telegram бота.

**Файлы:**
- `start.py` - Команда `/start`, регистрация пользователя
- `profile.py` - Профиль пользователя, редактирование
- `generations.py` - Создание и управление генерациями
- `shop.py` - Магазин пакетов

**Особенности:**
- Использует FSM (Finite State Machine) для многошаговых диалогов
- Обработка сообщений и callback-запросов
- Интеграция с сервисами и репозиториями

### 2. Services Layer (`app/services/`)

Бизнес-логика приложения.

**WavespeedService**
```python
- generate(prompt, model) -> Generation ID
- get_status(generation_id) -> Status
```

**GenerationService**
```python
- create_generation(user_id, prompt) -> Generation
- get_generation_status(generation_id) -> Status
```

**TelegramFilesService**
```python
- get_file_info(file_id) -> File metadata
- download_file(file_id, path) -> bool
```

### 3. Repository Layer (`app/db/repositories.py`)

CRUD операции по работе с БД.

```python
UserRepository
├── create_or_update()
├── get_by_telegram_id()
├── get_by_id()
└── update_balance()

GenerationRepository
├── create()
├── get_by_id()
└── update_status()

FileBlacklistRepository
├── add()
└── is_blacklisted()
```

### 4. Models Layer (`app/db/models.py`)

ORM модели для БД.

```
User
├── telegram_id (unique)
├── username
├── first_name
├── last_name
├── balance
├── is_premium
└── timestamps

Generation
├── user_id (FK)
├── prompt
├── result_url
├── status (pending/processing/completed/failed)
├── cost
└── timestamps

FileBlacklist
├── file_id (unique)
├── reason
└── timestamps
```

## Поток данных

### Пример: Команда /start

```
User sends /start
     ↓
Router handler: start_command()
     ↓
Check if user exists or create
     ↓
UserRepository.create_or_update()
     ↓
SQLAlchemy ORM → Database
     ↓
Send welcome message with balance
```

### Пример: Создание генерации

```
User sends prompt
     ↓
Router handler: process_prompt()
     ↓
Validate prompt and balance
     ↓
GenerationService.create_generation()
     ├─ GenerationRepository.create() → Database
     └─ WavespeedService.generate() → External API
     ↓
Return generation ID and start polling status
```

## Конфигурация

### Settings (pydantic-settings)

Все конфигурации загружаются из переменных окружения через `pydantic-settings`.

```python
# config.py
class Settings(BaseSettings):
    BOT_TOKEN: str
    DATABASE_URL: str
    WAVESPEED_API_KEY: str
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    ...
```

Переменные из `.env`:
```env
BOT_TOKEN=123:ABC...
DATABASE_URL=sqlite+aiosqlite:///./bot.db
WAVESPEED_API_KEY=key123...
LOG_LEVEL=INFO
```

## Асинхронность

Весь проект построен на асинхронности:

1. **Бот**: `aiogram` - асинхронный фреймворк
2. **БД**: SQLAlchemy async с `aiosqlite/asyncpg`
3. **HTTP**: `httpx` асинхронный клиент
4. **Event loop**: `asyncio` с graceful shutdown

### Graceful Shutdown

```python
# Обработка сигналов SIGTERM и SIGINT
loop.add_signal_handler(signal.SIGTERM, signal_handler)
loop.add_signal_handler(signal.SIGINT, signal_handler)

# Корректное закрытие:
- Отмена активных задач
- Закрытие сессии бота
- Закрытие соединений БД
```

## Логирование

Структурированное логирование со степенями важности:

```python
logger.debug()   # Детали для разработки
logger.info()    # Основные события
logger.warning() # Предупреждения
logger.error()   # Ошибки
```

Логирование настраивается в `app/utils/logging.py` и использует уровень из конфига.

## Обработка ошибок

### Стратегия обработки ошибок

1. **Валидация входных данных** - Проверка перед обработкой
2. **Try-Except блоки** - Ловля исключений на каждом слое
3. **Логирование** - Все ошибки логируются
4. **User feedback** - Понятные сообщения пользователю
5. **Recovery** - Система восстановления после ошибок

### Примеры

```python
try:
    user = await user_repo.get_by_telegram_id(user_id)
    if not user:
        raise ValueError("User not found")
    
    if user.balance < cost:
        raise ValueError("Insufficient balance")
    
    # Выполнить операцию
    
except ValueError as e:
    logger.warning(f"Validation error: {e}")
    await message.answer("❌ Validation error")
except Exception as e:
    logger.error(f"Unexpected error: {e}")
    await message.answer("❌ Unexpected error")
```

## Миграции БД (Alembic)

### Создание первой миграции

```bash
# Автоматическое определение изменений моделей
alembic revision --autogenerate -m "Initial migration"

# Применение миграций
alembic upgrade head

# Откат на предыдущую версию
alembic downgrade -1
```

Файлы миграций хранятся в `alembic/versions/`.

## Развертывание

### SQLite (разработка)
```env
DATABASE_URL=sqlite+aiosqlite:///./bot.db
```

### PostgreSQL (production)
```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/dbname
```

## Лучшие практики

1. ✅ **Асинхронность везде** - Используйте async/await
2. ✅ **Типизация** - Используйте type hints
3. ✅ **Логирование** - Логируйте важные события
4. ✅ **Обработка ошибок** - Ловите исключения
5. ✅ **Валидация** - Проверяйте входные данные
6. ✅ **DRY** - Не повторяйте код
7. ✅ **SOLID** - Следуйте принципам проектирования
8. ✅ **Чистый код** - Понятные имена и структура

## Масштабирование

### При увеличении нагрузки

1. **БД**: Перейти на PostgreSQL с connection pooling
2. **Кэш**: Добавить Redis для кэширования
3. **Очередь**: Использовать Celery для long-running tasks
4. **Load balance**: Запустить несколько инстансов бота
5. **Мониторинг**: Добавить Prometheus/Grafana

### Структура для масштабирования

```
app/
├── bot/               # Обработчики команд
├── services/          # Бизнес-логика
├── db/                # Доступ к данным
├── cache/             # Кэширование (Redis)
├── queue/             # Очередь задач (Celery)
├── middleware/        # Middleware слой
├── exceptions/        # Кастомные исключения
└── validators/        # Валидаторы данных
```

## Тестирование

### Unit tests (рекомендуется добавить)

```python
# tests/test_services.py
@pytest.mark.asyncio
async def test_create_generation():
    service = GenerationService(session, wavespeed_service)
    result = await service.create_generation(
        user_id=1,
        prompt="Test prompt"
    )
    assert result['generation_id'] is not None
```

### Integration tests

```python
# tests/test_bot.py
@pytest.mark.asyncio
async def test_start_command():
    # Полный сценарий с реальной БД
    pass
```
