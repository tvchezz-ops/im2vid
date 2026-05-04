"""Логирование."""
import logging
import sys
from typing import Optional


def setup_logging(name: Optional[str] = None, level: str = "INFO") -> logging.Logger:
    """Настроить логирование."""
    logger = logging.getLogger(name or __name__)
    
    # Избегаем дублирования обработчиков
    if logger.handlers:
        return logger
    
    logger.setLevel(level)
    
    # Форматтер
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Обработчик для консоли
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger


# Глобальный логгер
logger = setup_logging("telegram_bot")
