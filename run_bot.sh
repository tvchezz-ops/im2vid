#!/bin/bash
# Скрипт для запуска Telegram бота

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_BIN="$PROJECT_DIR/venv/bin/python"

# Переходим в папку проекта чтобы .env был найден
cd "$PROJECT_DIR"

export PYTHONPATH="$PROJECT_DIR:$PYTHONPATH"

exec "$VENV_BIN" -m app
