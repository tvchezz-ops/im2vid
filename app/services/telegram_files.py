"""Сервис для работы с файлами Telegram."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from aiogram import Bot
from aiogram.types import File

from app.config import settings

from app.utils import ImageUploadError, logger


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEDIA_DIR = PROJECT_ROOT / "media"
MEDIA_ROUTE_PREFIX = "/media"
MEDIA_BIND_HOST = "0.0.0.0"
MEDIA_BIND_PORT = 8080


def ensure_public_base_url() -> str:
    """Проверить, что публичный базовый URL задан."""
    public_base_url = settings.public_base_url.strip().rstrip("/")
    if not public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL должен быть задан для публикации Telegram-файлов")
    return public_base_url


def ensure_media_dir() -> Path:
    """Создать директорию media при необходимости."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return MEDIA_DIR


def build_public_media_url(filename: str) -> str:
    """Собрать публичный URL для сохраненного файла."""
    base_url = ensure_public_base_url()
    return f"{base_url}{MEDIA_ROUTE_PREFIX}/{quote(filename)}"


def create_media_app() -> web.Application:
    """Создать aiohttp-приложение для публикации статических media-файлов."""
    ensure_media_dir()
    app = web.Application()
    app.router.add_static(f"{MEDIA_ROUTE_PREFIX}/", path=str(MEDIA_DIR), show_index=False)
    return app


def cleanup_old_media_files(max_age_seconds: int = 24 * 60 * 60) -> int:
    """Удалить старые файлы из media-директории."""
    media_dir = ensure_media_dir()
    deleted_count = 0
    cutoff = time.time() - max_age_seconds

    for path in media_dir.iterdir():
        if not path.is_file():
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        path.unlink(missing_ok=True)
        deleted_count += 1

    if deleted_count:
        logger.info("Removed %s stale media files", deleted_count)
    return deleted_count


def _detect_file_suffix(file: File) -> str:
    """Определить расширение файла по Telegram file_path."""
    suffix = Path(file.file_path or "").suffix.lower()
    return suffix or ".jpg"


class TelegramFilesService:
    """Сервис для работы с файлами в Telegram."""

    def __init__(self, bot: Bot):
        """Инициализация."""
        self.bot = bot

    async def get_file_info(self, file_id: str) -> File:
        """Получить информацию о файле."""
        try:
            file = await self.bot.get_file(file_id)
            logger.debug(f"Got file info: {file_id}")
            return file
        except Exception as e:
            logger.exception("Error getting Telegram file info: %s", e)
            raise ImageUploadError(
                "Не удалось загрузить изображение. Отправьте другое и попробуйте снова.",
                log_message=f"Telegram get_file failed for {file_id}: {e}",
            ) from e

    async def download_file(self, file_id: str, destination_path: str) -> bool:
        """Скачать файл."""
        try:
            file = await self.get_file_info(file_id)
            await self.bot.download_file(file.file_path, destination_path)
            logger.info(f"File downloaded: {file_id}")
            return True
        except Exception as e:
            logger.exception("Error downloading Telegram file: %s", e)
            return False

    async def save_telegram_file_and_get_public_url(self, file_id: str) -> str:
        """Сохранить Telegram-файл локально и вернуть публичный URL."""
        ensure_public_base_url()
        file = await self.get_file_info(file_id)
        media_dir = ensure_media_dir()
        filename = f"{uuid.uuid4().hex}{_detect_file_suffix(file)}"
        destination = media_dir / filename
        try:
            await self.bot.download_file(file.file_path, destination=str(destination))
        except Exception as e:
            logger.exception("Error saving Telegram file locally: %s", e)
            raise ImageUploadError(
                "Не удалось загрузить изображение. Отправьте другое и попробуйте снова.",
                log_message=f"Telegram file download failed for {file_id}: {e}",
            ) from e
        logger.info("Telegram file %s saved to %s", file_id, destination)
        return build_public_media_url(filename)
