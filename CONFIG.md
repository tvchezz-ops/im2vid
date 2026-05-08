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
INSTANCE_NAME=local-dev
TEMP_MEDIA_DIR=tmp/media
TEMP_MEDIA_TTL_MINUTES=30
MAX_PARALLEL_GENERATIONS_PER_USER=3
WAVESPEED_POLL_FAST_SECONDS=30
WAVESPEED_POLL_NORMAL_SECONDS=60
WAVESPEED_POLL_SLOW_SECONDS=120
WAVESPEED_POLL_TIMEOUT_SECONDS=1800
TELEGRAM_STARS_RETURN_BOT_USERNAME=
TELEGRAM_STARS_WEBHOOK_SECRET=
WALLET_BOT_USERNAME=
MAIN_BOT_USERNAME=
NOWPAYMENTS_API_KEY=
NOWPAYMENTS_IPN_SECRET=
NOWPAYMENTS_BASE_URL=https://api.nowpayments.io
NOWPAYMENTS_SUCCESS_URL=
NOWPAYMENTS_CANCEL_URL=
CREDIT_USD_PRICE=0.013
STORE_INPUT_MEDIA=false
STORE_OUTPUT_URLS=false
```

## 🔧 Поля Settings класса

| Поле | Тип | Обязательная | Значение по умолчанию | Описание |
|------|-----|-------------|----------------------|---------|
| `bot_token` | `str` | ✅ | — | Токен Telegram бота от @BotFather |
| `instance_name` | `str` | ❌ | `""` | Имя deployment/instance для startup-логов и диагностики конфликтов polling |
| `wavespeed_api_key` | `str` | ✅ | — | API ключ для сервиса генерации |
| `database_url` | `str` | ❌ | `sqlite+aiosqlite:///./bot.db` | Строка подключения к БД |
| `public_base_url` | `str` | ✅ | — | Публичный URL для временно опубликованных media-файлов |
| `admin_ids` | `list[int]` | ❌ | `[]` | Список админ ID через запятую |
| `temp_media_dir` | `str` | ❌ | `tmp/media` | Временная директория для входных изображений |
| `temp_media_ttl_minutes` | `int` | ❌ | `30` | TTL временных файлов на диске |
| `max_parallel_generations_per_user` | `int` | ❌ | `3` | Максимум активных `generation_request` со статусами `created`, `pending`, `processing` на пользователя |
| `wavespeed_poll_fast_seconds` | `int` | ❌ | `30` | Интервал polling Wavespeed в первые 5 минут |
| `wavespeed_poll_normal_seconds` | `int` | ❌ | `60` | Интервал polling Wavespeed после 5 минут |
| `wavespeed_poll_slow_seconds` | `int` | ❌ | `120` | Интервал polling Wavespeed после 15 минут |
| `wavespeed_poll_timeout_seconds` | `int` | ❌ | `1800` | Общий timeout polling Wavespeed в секундах |
| `telegram_stars_return_bot_username` | `str` | ❌ | `""` | Username текущего бота для возврата из wallet bot без `@` |
| `telegram_stars_webhook_secret` | `str` | ❌ | `""` | Секрет `X-Webhook-Secret` для `POST /webhooks/stars-wallet` |
| `wallet_bot_username` | `str | None` | ❌ | `None` | Username внешнего wallet bot для Telegram Stars; `@` и пробелы нормализуются |
| `main_bot_username` | `str` | ❌ | `""` | Username основного бота без `@` для возврата после внешних оплат |
| `nowpayments_api_key` | `str` | ❌ | `""` | NOWPayments API key для crypto top-ups |
| `nowpayments_ipn_secret` | `str` | ❌ | `""` | NOWPayments IPN secret для проверки webhook signature |
| `nowpayments_base_url` | `str` | ❌ | `https://api.nowpayments.io` | NOWPayments API base URL |
| `nowpayments_success_url` | `str` | ❌ | `""` | Optional success redirect от NOWPayments |
| `nowpayments_cancel_url` | `str` | ❌ | `""` | Optional cancel redirect от NOWPayments |
| `credit_usd_price` | `Decimal` | ❌ | `0.013` | USD цена одного кредита для crypto top-ups |
| `store_input_media` | `bool` | ❌ | `false` | Всегда `false`, поле совместимости |
| `store_output_urls` | `bool` | ❌ | `false` | Всегда `false`, поле совместимости |

## 📝 Пример `.env` файла

```env
# Обязательные переменные
BOT_TOKEN=your_bot_token_here
WAVESPEED_API_KEY=your_api_key_here
PUBLIC_BASE_URL=https://your-public-host.example.com

# Опциональные переменные
DATABASE_URL=sqlite+aiosqlite:///./bot.db
ADMIN_IDS=123456789,987654321,111111111
INSTANCE_NAME=local-dev
TEMP_MEDIA_DIR=tmp/media
TEMP_MEDIA_TTL_MINUTES=30
MAX_PARALLEL_GENERATIONS_PER_USER=3
WAVESPEED_POLL_FAST_SECONDS=30
WAVESPEED_POLL_NORMAL_SECONDS=60
WAVESPEED_POLL_SLOW_SECONDS=120
WAVESPEED_POLL_TIMEOUT_SECONDS=1800
TELEGRAM_STARS_RETURN_BOT_USERNAME=
TELEGRAM_STARS_WEBHOOK_SECRET=
WALLET_BOT_USERNAME=
MAIN_BOT_USERNAME=
NOWPAYMENTS_API_KEY=
NOWPAYMENTS_IPN_SECRET=
NOWPAYMENTS_BASE_URL=https://api.nowpayments.io
NOWPAYMENTS_SUCCESS_URL=
NOWPAYMENTS_CANCEL_URL=
CREDIT_USD_PRICE=0.013
STORE_INPUT_MEDIA=false
STORE_OUTPUT_URLS=false
```

## Telegram Stars wallet bot

Основной бот не отправляет Telegram Stars invoice. После выбора суммы он создает payment order и отправляет пользователя во внешний wallet bot. Начисление происходит только после verified callback на `POST /webhooks/stars-wallet`.

После выбора пакета Stars бот показывает экран с кнопкой `Перейти к оплате ⭐` во внешний wallet bot. Fallback invoice в основном боте не используется.

Возврат из wallet bot должен вести на `https://t.me/{TELEGRAM_STARS_RETURN_BOT_USERNAME}?start=paid_{payload}`. Такой возврат сам по себе не подтверждает оплату: бот показывает `Проверяем оплату...` и начисляет кредиты только если заказ уже был подтвержден wallet webhook через `POST /webhooks/stars-wallet` с корректным `X-Webhook-Secret`.

## NOWPayments crypto payments

Crypto top-ups используют только NOWPayments hosted checkout. После выбора пакета бот создает `provider="nowpayments"` payment order, отправляет запрос в NOWPayments invoice API и показывает пользователю только кнопку `Оплатить через NOWPayments`. Выбор валюты и сети происходит на стороне NOWPayments; бот не показывает адреса кошельков, сети или transaction hash.

Для production нужны `NOWPAYMENTS_API_KEY` и `NOWPAYMENTS_IPN_SECRET`. `NOWPAYMENTS_BASE_URL` можно оставить по умолчанию. `NOWPAYMENTS_SUCCESS_URL` и `NOWPAYMENTS_CANCEL_URL` опциональны. IPN callback всегда строится как `PUBLIC_BASE_URL/webhooks/nowpayments`.

Webhook `POST /webhooks/nowpayments` проверяет `x-nowpayments-sig`. Только статусы `finished` и `confirmed` начисляют кредиты идемпотентно; `failed` помечает заказ failed, `expired` помечает expired; `waiting`, `confirming`, `sending` остаются pending.

## 🚀 Использование конфигурации в коде

```python
from app.config import settings

# Получить значения
token = settings.bot_token
api_key = settings.wavespeed_api_key
db_url = settings.database_url
public_base_url = settings.public_base_url
temp_media_dir = settings.temp_media_dir

# Получить список админов (парсится из строки)
admin_ids = settings.admin_ids  # -> [123456789, 987654321, 111111111]
```

## No media storage policy

- Входные изображения скачиваются только во временную директорию `TEMP_MEDIA_DIR`.
- В БД не сохраняются `input_image_file_ids`, `input_image_urls` и `output_urls`.
- Результаты Wavespeed не скачиваются на сервер.
- После завершения или отмены генерации временный входной файл удаляется.

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
