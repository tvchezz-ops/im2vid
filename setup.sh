#!/bin/bash
# Скрипт для быстрого запуска разработки

set -e

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Telegram Bot Setup ===${NC}"

# Проверка Python версии
echo -e "${YELLOW}Checking Python version...${NC}"
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $python_version"

# Создание виртуального окружения
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

# Активирование виртуального окружения
echo -e "${YELLOW}Activating virtual environment...${NC}"
source venv/bin/activate

# Установка зависимостей
echo -e "${YELLOW}Installing dependencies...${NC}"
pip install -q -r requirements.txt

# Копирование .env файла если его нет
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}Creating .env file...${NC}"
    cp .env.example .env
    echo -e "${GREEN}!!! Please update .env with your settings !!!${NC}"
fi

echo -e "${GREEN}Setup completed!${NC}"
echo -e "${YELLOW}To start the bot, run:${NC}"
echo -e "${GREEN}python -m app.main${NC}"
