# Telegram Bot with aiogram 3

Telegram-бот на aiogram 3 с поддержкой асинхронной базы данных, миграций и интеграцией с Wavespeed API.

## Требования

- Python 3.11+
- PostgreSQL или SQLite для локальной разработки

## Установка

### 1. Клонировать репозиторий и установить зависимости

```bash
git clone <repo-url>
cd telegram_bot
python -m venv venv
source venv/bin/activate  # для Linux/macOS
# или
venv\Scripts\activate  # для Windows

pip install -r requirements.txt
```

### 2. Настроить переменные окружения

```bash
cp .env.example .env
# Отредактировать .env файл со своими значениями
```

### 3. Инициализировать базу данных (для PostgreSQL)

```bash
# Создать первую миграцию
alembic revision --autogenerate -m "Initial migration"

# Применить миграции
alembic upgrade head
```

### 4. Запустить бота

```bash
python -m app.main
```

## Структура проекта

```
telegram_bot/
├── app/
│   ├── __init__.py
│   ├── main.py              # Точка входа приложения
│   ├── config.py            # Конфигурация приложения
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── routers/         # Обработчики команд
│   │   │   ├── __init__.py
│   │   │   ├── start.py     # Команда /start
│   │   │   ├── profile.py   # Профиль пользователя
│   │   │   ├── generations.py # Генерации
│   │   │   └── shop.py      # Магазин
│   │   ├── keyboards.py     # Кнопки
│   │   └── states.py        # FSM состояния
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py          # Базовый класс моделей
│   │   ├── session.py       # Сессии БД
│   │   ├── models.py        # Модели БД
│   │   └── repositories.py  # Репозитории (CRUD операции)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── wavespeed.py     # Интеграция с Wavespeed API
│   │   ├── telegram_files.py # Работа с файлами Telegram
│   │   └── generation_service.py # Сервис генераций
│   └── utils/
│       ├── __init__.py
│       └── logging.py       # Логирование
├── alembic/                 # Миграции БД
│   ├── env.py
│   ├── script.py.mako
│   ├── alembic.ini
│   └── versions/
├── .env.example             # Пример переменных окружения
├── requirements.txt         # Зависимости проекта
└── README.md
```

## Функции

- ✅ Асинхронная база данных (SQLite для разработки, PostgreSQL для продакшена)
- ✅ Миграции с Alembic
- ✅ Система состояний FSM для многошаговых диалогов
- ✅ Интеграция с Wavespeed API
- ✅ Структурированная архитектура с разделением ответственности
- ✅ Логирование и обработка ошибок
- ✅ Graceful shutdown

## Использование

### Добавление нового роутера

Создайте файл в `app/bot/routers/`:

```python
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()

@router.message(Command("mycommand"))
async def my_command(message: Message):
    await message.answer("Hello!")
```

Добавьте импорт в `app/bot/routers/__init__.py`:

```python
from .myrouter import router as my_router
```

## Переменные окружения

See `.env.example` для полного списка доступных переменных.

Для сценария генерации `PUBLIC_BASE_URL` обязателен: бот временно скачивает входное изображение в `TEMP_MEDIA_DIR` и публикует его через встроенный static endpoint `aiohttp` на `http://127.0.0.1:${PORT:-8080}/media/...`.
Если Wavespeed должен забрать файл извне, `PUBLIC_BASE_URL` должен указывать не на localhost, а на публичный URL туннеля или reverse proxy, который ведет на этот локальный endpoint.
На Railway media server слушает порт из переменной окружения `PORT`; локально без `PORT` используется fallback `8080`.

Дополнительные переменные:

```env
TEMP_MEDIA_DIR=tmp/media
TEMP_MEDIA_TTL_MINUTES=30
INSTANCE_NAME=local-dev
STORE_INPUT_MEDIA=false
STORE_OUTPUT_URLS=false
MAIN_BOT_USERNAME=
TELEGRAM_STARS_RETURN_BOT_USERNAME=
TELEGRAM_STARS_WEBHOOK_SECRET=
WALLET_BOT_USERNAME=
NOWPAYMENTS_API_KEY=
NOWPAYMENTS_IPN_SECRET=
NOWPAYMENTS_BASE_URL=https://api.nowpayments.io
NOWPAYMENTS_SUCCESS_URL=
NOWPAYMENTS_CANCEL_URL=
CREDIT_USD_PRICE=0.013
WAVESPEED_POLL_FAST_SECONDS=10
WAVESPEED_POLL_NORMAL_SECONDS=30
WAVESPEED_POLL_SLOW_SECONDS=60
WAVESPEED_POLL_TIMEOUT_SECONDS=1800
PRICING_MARKUP_MULTIPLIER=2
USD_PER_100_CREDITS=1.30
```

## Crypto Payments With NOWPayments

Crypto top-ups use only NOWPayments hosted checkout. The balance top-up screen shows a `₿ Crypto` option, lets the user select a credit package, creates a NOWPayments invoice, and shows only the checkout button. The user chooses currency and network on the NOWPayments page; the bot never displays wallet addresses, networks, or transaction hashes.

Required production env:

- `NOWPAYMENTS_API_KEY`
- `NOWPAYMENTS_IPN_SECRET`
- `NOWPAYMENTS_BASE_URL`, default `https://api.nowpayments.io`
- `NOWPAYMENTS_SUCCESS_URL`, optional success redirect from NOWPayments
- `NOWPAYMENTS_CANCEL_URL`, optional cancel redirect from NOWPayments
- `CREDIT_USD_PRICE`, default `0.013`
- `PRICING_MARKUP_MULTIPLIER`, default `2`
- `USD_PER_100_CREDITS`, default `1.30`

The webhook endpoint is `POST /webhooks/nowpayments`. It verifies `x-nowpayments-sig` with HMAC SHA512. Credits are added only when a verified webhook has `payment_status == finished` or `confirmed`; client-side callbacks and return links are never treated as proof of payment. Repeated webhooks are idempotent and do not add credits twice.

## Telegram Stars Wallet Payments

Telegram Stars payments are created in the main bot but paid in the external wallet bot. After a user selects a Stars amount, the main bot creates a `payment_order` with `status="created"` and sends a URL button to `https://t.me/{WALLET_BOT_USERNAME}?start={payload}`. Returning to the main bot never credits the balance by itself.

The trusted wallet callback endpoint is `POST /webhooks/stars-wallet`. It requires `X-Webhook-Secret` to match `TELEGRAM_STARS_WEBHOOK_SECRET`, checks the payload and amount, credits only once, stores `external_payment_id`, and notifies the user.

Для больших результатов генерации бот показывает короткую ссылку вида `PUBLIC_BASE_URL/d/{token}`, а не прямой длинный Cloudflare R2 presigned URL. По этой короткой ссылке пользователь сначала попадает на отдельную HTML-страницу скачивания с названием файла, сроком действия ссылки и кнопкой `Скачать файл`; только route `PUBLIC_BASE_URL/d/{token}/download` генерирует свежий временный signed URL и делает redirect. В базе хранится только token, `r2_object_key`, имя файла, размер, content type, срок жизни и счётчик использований; полный signed URL не сохраняется и не логируется.

## No Media Storage Policy

- Бот не хранит входные изображения пользователей на постоянной основе.
- Бот не хранит результаты генерации на сервере.
- Бот не сохраняет URL результатов Wavespeed в базе данных.
- Временный файл нужен только для того, чтобы Wavespeed смог забрать входное изображение по публичному URL.
- После завершения генерации временный входной файл удаляется независимо от результата: `completed`, `failed`, `timeout` или `cancel`.

## TelegramConflictError

Если появляется `TelegramConflictError`, это означает, что один и тот же `BOT_TOKEN` используется сразу в двух местах.

Типичный лог выглядит так: `Conflict: terminated by other getUpdates request; make sure that only one bot instance is running`. Это не чинится кодом полностью: нужно убрать второй запущенный экземпляр, который читает updates тем же токеном.

Что делать:

- Остановить локально запущенный бот, если он уже работает.
- Проверить Railway и убедиться, что с этим токеном работает только один active service или один active deployment.
- Если есть лишний Railway service с тем же `BOT_TOKEN`, остановить или удалить его.
- Если раньше использовался webhook, удалить его и сбросить очередь обновлений:

```bash
curl -X POST "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
```

Та же команда как URL для ручного запуска:

```text
https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook?drop_pending_updates=true
```

- Для одного `BOT_TOKEN` должен оставаться только один активный polling/webhook consumer.
- Для диагностики можно задать `INSTANCE_NAME`, например `railway-main` или `local-dev`; при старте бот логирует `instance_name`, Telegram bot id и username.

## Лицензия

MIT
