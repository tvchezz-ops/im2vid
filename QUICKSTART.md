# QUICKSTART.md - Быстрый старт

## ⚡ За 5 минут до работающего бота

### Шаг 1: Клонировать и войти в проект

```bash
cd telegram_bot
python -m venv venv
source venv/bin/activate  # Linux/macOS
# или для Windows:
# venv\Scripts\activate
```

### Шаг 2: Установить зависимости

```bash
pip install -r requirements.txt
```

### Шаг 3: Настроить конфигурацию

```bash
cp .env.example .env
```

Отредактировать `.env`:
```env
BOT_TOKEN=<ваш_токен_от_BotFather>
WAVESPEED_API_KEY=<ваш_API_ключ>
```

### Шаг 4: Запустить бота

```bash
python -m app.main
```

**Вот и все! Бот готов к использованию.**

---

## 📋 Получить BOT_TOKEN

1. Откройте Telegram
2. Найдите `@BotFather`
3. Отправьте `/newbot`
4. Следуйте инструкциям
5. Скопируйте полученный токен в `.env`

---

## 🗄️ Работа с БД

### Для SQLite (рекомендуется для разработки)

Ничего не нужно делать! Просто запустите бота и БД создастся автоматически.

### Для PostgreSQL (для production)

1. Установить PostgreSQL
2. Создать БД:
```sql
CREATE DATABASE telegram_bot;
```

3. Обновить `.env`:
```env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/telegram_bot
```

4. Применить миграции:
```bash
alembic upgrade head
```

---

## 📝 Команды для разработки

### Создать миграцию (для PostgreSQL)
```bash
alembic revision --autogenerate -m "Описание изменений"
```

### Применить миграции
```bash
alembic upgrade head
```

### Откатить миграцию
```bash
alembic downgrade -1
```

### Проверить синтаксис Python
```bash
python -m py_compile app/*.py
```

---

## 🔧 Структура после запуска

```
telegram_bot/
├── venv/                # Виртуальное окружение
├── bot.db              # Базу данных SQLite (создается автоматически)
├── app/
│   ├── main.py         # Главный файл бота
│   ├── config.py       # Конфигурация
│   ├── bot/            # Обработчики команд
│   ├── db/             # Модели и репозитории
│   ├── services/       # Бизнес-логика
│   └── utils/          # Утилиты
├── alembic/            # Миграции БД
├── .env                # Переменные окружения (локально)
└── requirements.txt    # Зависимости
```

---

## 🧪 Тестирование

Отправьте боту в Telegram команду `/start` - должна сработать.

### Что проверяется

✅ Бот отвечает на `/start`
✅ Создает пользователя в БД
✅ Показывает баланс
✅ Кнопки в меню работают

---

## 🚀 Развертывание

### На локальной машине

```bash
python -m app.main
```

### На сервере (systemd)

1. Создать сервис:
```bash
sudo nano /etc/systemd/system/telegram-bot.service
```

2. Добавить:
```ini
[Unit]
Description=Telegram Bot
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/telegram_bot
Environment="PATH=/path/to/telegram_bot/venv/bin"
ExecStart=/path/to/telegram_bot/venv/bin/python -m app.main
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

3. Включить сервис:
```bash
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

---

## 📚 Дополнительная документация

- [ARCHITECTURE.md](ARCHITECTURE.md) - Архитектура проекта
- [EXAMPLES.md](EXAMPLES.md) - Примеры использования

---

## 🆘 Часто встречающиеся проблемы

### Ошибка: "aiogram: command not found"

→ Установите зависимости: `pip install -r requirements.txt`

### Ошибка: "BOT_TOKEN is not set"

→ Создайте `.env` файл и добавьте BOT_TOKEN

### Ошибка: "Database locked"

→ Это normale для SQLite в разработке. Для production используйте PostgreSQL.

### Бот не отвечает

→ Проверьте:
1. BOT_TOKEN правильный
2. Интернет соединение
3. Нет ошибок в логах

---

## 📞 Поддержка

Если что-то не работает:
1. Проверьте логи в консоли
2. Посмотрите примеры в [EXAMPLES.md](EXAMPLES.md)
3. Проверьте архитектуру в [ARCHITECTURE.md](ARCHITECTURE.md)

Happy coding! 🎉
