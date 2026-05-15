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
ENV=development
DATABASE_URL=sqlite+aiosqlite:///./bot.db
DATABASE_PRIVATE_URL=
DATABASE_PUBLIC_FALLBACK_ENABLED=false
STRICT_PRIVATE_NETWORK=false
ADMIN_IDS=123456789,987654321    # Список ID админов через запятую
INSTANCE_NAME=local-dev
TEMP_MEDIA_DIR=tmp/media
TEMP_MEDIA_TTL_MINUTES=30
WAVESPEED_POLL_FAST_SECONDS=10
WAVESPEED_POLL_NORMAL_SECONDS=30
WAVESPEED_POLL_SLOW_SECONDS=60
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
PRICING_MARKUP_MULTIPLIER=1.5
USD_PER_100_CREDITS=1.30
STORE_INPUT_MEDIA=false
STORE_OUTPUT_URLS=false
```

## 🔧 Поля Settings класса

| Поле | Тип | Обязательная | Значение по умолчанию | Описание |
|------|-----|-------------|----------------------|---------|
| `bot_token` | `str` | ✅ | — | Токен Telegram бота от @BotFather |
| `env` | `str` | ❌ | `development` | Окружение запуска: `development`, `production` |
| `instance_name` | `str` | ❌ | `""` | Имя deployment/instance для startup-логов и диагностики конфликтов polling |
| `wavespeed_api_key` | `str` | ✅ | — | API ключ для сервиса генерации |
| `database_url` | `str` | ❌ | `sqlite+aiosqlite:///./bot.db` | Главный override строки подключения к БД |
| `database_private_url` | `str` | ❌ | `""` | Private Railway Postgres URL, используется если `DATABASE_URL` не задан |
| `database_public_fallback_enabled` | `bool` | ❌ | `false` | Разрешить fallback на public endpoint только явно |
| `strict_private_network` | `bool` | ❌ | `false` | В production запрещать public Railway DB endpoints |
| `public_base_url` | `str` | ✅ | — | Публичный URL для временно опубликованных media-файлов |
| `admin_ids` | `list[int]` | ❌ | `[]` | Список админ ID через запятую |
| `temp_media_dir` | `str` | ❌ | `tmp/media` | Временная директория для входных изображений |
| `temp_media_ttl_minutes` | `int` | ❌ | `30` | TTL временных файлов на диске |
| `wavespeed_poll_fast_seconds` | `int` | ❌ | `10` | Интервал polling Wavespeed в первые 3 минуты |
| `wavespeed_poll_normal_seconds` | `int` | ❌ | `30` | Интервал polling Wavespeed с 3 до 10 минут |
| `wavespeed_poll_slow_seconds` | `int` | ❌ | `60` | Интервал polling Wavespeed после 10 минут |
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
| `pricing_markup_multiplier` | `Decimal` | ❌ | `1.5` | Множитель на цену провайдера для расчёта стоимости генерации |
| `usd_per_100_credits` | `Decimal` | ❌ | `1.30` | USD цена 100 кредитов для пересчёта стоимости генерации |
| `referral_referrer_bonus_credits` | `int` | ❌ | `5` | Кредиты пригласившему за нового реферала |
| `referral_referred_bonus_credits` | `int` | ❌ | `0` | Кредиты новому пользователю за вход по реферальной ссылке |
| `store_input_media` | `bool` | ❌ | `false` | Всегда `false`, поле совместимости |
| `store_output_urls` | `bool` | ❌ | `false` | Всегда `false`, поле совместимости |

## 📝 Пример `.env` файла

```env
# Обязательные переменные
BOT_TOKEN=your_bot_token_here
WAVESPEED_API_KEY=your_api_key_here
PUBLIC_BASE_URL=https://your-public-host.example.com

# Опциональные переменные
ENV=development
DATABASE_URL=sqlite+aiosqlite:///./bot.db
DATABASE_PRIVATE_URL=
DATABASE_PUBLIC_FALLBACK_ENABLED=false
STRICT_PRIVATE_NETWORK=false
ADMIN_IDS=123456789,987654321,111111111
INSTANCE_NAME=local-dev
TEMP_MEDIA_DIR=tmp/media
TEMP_MEDIA_TTL_MINUTES=30
WAVESPEED_POLL_FAST_SECONDS=10
WAVESPEED_POLL_NORMAL_SECONDS=30
WAVESPEED_POLL_SLOW_SECONDS=60
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
PRICING_MARKUP_MULTIPLIER=1.5
USD_PER_100_CREDITS=1.30
REFERRAL_REFERRER_BONUS_CREDITS=5
REFERRAL_REFERRED_BONUS_CREDITS=0
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

### Railway Postgres через private networking

На Railway production backend должен подключаться к Postgres через private networking, а не через TCP proxy/public endpoint. Это убирает warning Railway `Connecting to a public endpoint will incur egress fees` и не гонит трафик через публичный egress.

Порядок выбора URL БД:

1. `DATABASE_URL`
2. `DATABASE_PRIVATE_URL`
3. `DATABASE_PUBLIC_URL`, только если явно включить `DATABASE_PUBLIC_FALLBACK_ENABLED=true`
4. `sqlite+aiosqlite:///./bot.db` для локальной разработки

Production env для Railway:

```env
ENV=production
DATABASE_PRIVATE_URL=postgresql+asyncpg://postgres:password@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/railway
DATABASE_PUBLIC_FALLBACK_ENABLED=false
STRICT_PRIVATE_NETWORK=true
```

Можно использовать уже раскрытый private hostname:

```env
DATABASE_PRIVATE_URL=postgresql+asyncpg://postgres:password@postgres.railway.internal:5432/railway
```

В production не задавайте public Railway TCP proxy URL как основной database config. Если выбранный URL всё же указывает на public Railway endpoint, backend пишет warning log `database_public_endpoint_warning`; при `STRICT_PRIVATE_NETWORK=true` startup падает. При старте также пишется diagnostic log:

```python
{"action": "database_connection_mode", "mode": "private", "host": "postgres.railway.internal"}
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
- DATABASE_URL (опционально, главный override строки подключения к БД)
- DATABASE_PRIVATE_URL (опционально, preferred Railway private networking URL)
- STRICT_PRIVATE_NETWORK (опционально, по умолчанию false)
- ADMIN_IDS (опционально)
```

## 📚 Дополнительно

- [pydantic-settings документация](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [`.env.example`](.env.example) - Примеры всех переменных
- [`app/config.py`](app/config.py) - Реализация Settings класса
