# Настройка конфигурации через pydantic-settings

## 📋 Структура конфигурации

Проект использует **pydantic-settings 2.x** для загрузки переменных окружения из `.env` файла.

### Обязательные переменные

```env
BOT_TOKEN=<ваш_токен_от_BotFather>
WAVESPEED_API_KEY=<ваш_api_ключ>
PUBLIC_BASE_URL=https://your-public-host.example.com
```

### Опциональные переменные с значениями по умолчанию

```env
DATABASE_URL=sqlite+aiosqlite:///./bot.db
ADMIN_IDS=123456789,987654321    # Список ID админов через запятую
```

## 🔧 Поля Settings класса

| Поле | Тип | Обязательная | Значение по умолчанию | Описание |
|------|-----|-------------|----------------------|---------|
| `bot_token` | `str` | ✅ | — | Токен Telegram бота от @BotFather |
| `wavespeed_api_key` | `str` | ✅ | — | API ключ для сервиса генерации |
| `database_url` | `str` | ❌ | `sqlite+aiosqlite:///./bot.db` | Строка подключения к БД |
| `public_base_url` | `str` | ✅ | — | Публичный URL для локально опубликованных media-файлов |
| `admin_ids` | `list[int]` | ❌ | `[]` | Список админ ID через запятую |

## 📝 Пример `.env` файла

```env
# Обязательные переменные
BOT_TOKEN=your_bot_token_here
WAVESPEED_API_KEY=your_api_key_here
PUBLIC_BASE_URL=https://your-public-host.example.com

# Опциональные переменные
DATABASE_URL=sqlite+aiosqlite:///./bot.db
ADMIN_IDS=123456789,987654321,111111111
```

## 🚀 Использование конфигурации в коде

```python
from app.config import settings

# Получить значения
token = settings.bot_token
api_key = settings.wavespeed_api_key
db_url = settings.database_url
public_base_url = settings.public_base_url

# Получить список админов (парсится из строки)
admin_ids = settings.admin_ids  # -> [123456789, 987654321, 111111111]
```

## 🔐 Безопасность

### ✅ Правильно

```python
# ✅ Загружать из .env
BOT_TOKEN = settings.bot_token

# ✅ Никогда не коммитить .env в Git
# (.gitignore уже настроен)
```

### ❌ Неправильно

```python
# ❌ Хардкодить значения
BOT_TOKEN = "123456:example"

# ❌ Коммитить .env файл
git add .env
```

## 🛠️ Развертывание

### Локальная разработка (SQLite)

```bash
cp .env.example .env
# Отредактировать .env с вашими значениями
python -m app.main
```

### Production (PostgreSQL)

1. Создать БД:
```sql
CREATE DATABASE telegram_bot;
```

2. Обновить `.env`:
```env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/telegram_bot
BOT_TOKEN=...
WAVESPEED_API_KEY=...
PUBLIC_BASE_URL=https://your-public-host.example.com
```

3. Запустить бота:
```bash
python -m app.main
```

### Docker

```dockerfile
FROM python:3.11

WORKDIR /app
COPY . .

RUN pip install -r requirements.txt

CMD ["python", "-m", "app.main"]
```

```bash
docker run --env-file .env telegram-bot
```

## 🔄 Загрузка переменных

Порядок приоритета (от высшего к низшему):

1. Переменные окружения (уже установленные в системе) `export BOT_TOKEN=...`
2. Значения из `.env` файла
3. Значения по умолчанию в коде

## ❌ Обработка ошибок при загрузке

Если отсутствуют обязательные переменные, вы получите понятное сообщение об ошибке:

```
❌ Ошибка при загрузке конфигурации:
2 validation errors for Settings
bot_token
  Field required [type=missing, input_value={}, input_type=dict]
wavespeed_api_key
  Field required [type=missing, input_value={}, input_type=dict]

Убедитесь, что в .env файле заданы все обязательные переменные:
- BOT_TOKEN
- WAVESPEED_API_KEY
- PUBLIC_BASE_URL
- DATABASE_URL (опционально, есть значение по умолчанию)
- ADMIN_IDS (опционально)
```

## 📚 Дополнительно

- [pydantic-settings документация](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [`.env.example`](.env.example) - Примеры всех переменных
- [`app/config.py`](app/config.py) - Реализация Settings класса
