"""Сервис для работы с файлами Telegram."""
from __future__ import annotations

from dataclasses import dataclass
import time
import uuid
from pathlib import Path

from aiohttp import web
from aiogram import Bot
from aiogram.types import File

from app.config import settings

from app.utils import ImageUploadError, logger


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEDIA_ROUTE_PREFIX = "/media"
MEDIA_BIND_HOST = "0.0.0.0"


@dataclass(frozen=True)
class TemporaryTelegramMedia:
    """Временный локальный файл Telegram с публичным URL."""

    local_path: Path
    public_url: str


def ensure_public_base_url() -> str:
    """Проверить, что публичный базовый URL задан."""
    public_base_url = settings.public_base_url.strip().rstrip("/")
    if not public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL должен быть задан для публикации Telegram-файлов")
    return public_base_url


def get_temp_media_dir() -> Path:
    """Получить путь к директории временных media-файлов."""
    configured_path = Path(settings.temp_media_dir)
    if configured_path.is_absolute():
        return configured_path
    return PROJECT_ROOT / configured_path


def ensure_temp_media_dir() -> Path:
    """Создать директорию временных media-файлов при необходимости."""
    temp_media_dir = get_temp_media_dir()
    temp_media_dir.mkdir(parents=True, exist_ok=True)
    return temp_media_dir


def build_public_media_url(filename: str) -> str:
    """Собрать публичный URL для временного файла."""
    base_url = ensure_public_base_url()
    return f"{base_url}{build_public_media_path(filename)}"


def build_public_media_path(filename: str) -> str:
    """Собрать public path для временного файла."""
    return f"{MEDIA_ROUTE_PREFIX}/{filename}"


def create_media_app() -> web.Application:
    """Создать aiohttp-приложение для публикации временных media-файлов."""
    from app.services.download_links import (
        DOWNLOAD_LINK_SERVICE_APP_KEY,
        DOWNLOAD_ROUTE_PREFIX,
        DownloadLinkService,
        handle_download_landing,
        handle_download_redirect,
    )

    temp_media_dir = ensure_temp_media_dir()
    app = web.Application()
    app[DOWNLOAD_LINK_SERVICE_APP_KEY] = DownloadLinkService()
    app.router.add_get(f"{DOWNLOAD_ROUTE_PREFIX}/{{token}}", handle_download_landing)
    app.router.add_get(f"{DOWNLOAD_ROUTE_PREFIX}/{{token}}/download", handle_download_redirect)
    app.router.add_static(f"{MEDIA_ROUTE_PREFIX}/", path=str(temp_media_dir), show_index=False)
    return app


def cleanup_old_temp_media_files(max_age_seconds: int | None = None) -> int:
    """Удалить старые файлы из директории временных media-файлов."""
    temp_media_dir = ensure_temp_media_dir()
    deleted_count = 0
    ttl_seconds = max_age_seconds or settings.temp_media_ttl_minutes * 60
    cutoff = time.time() - ttl_seconds

    for path in temp_media_dir.iterdir():
        if not path.is_file():
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        path.unlink(missing_ok=True)
        deleted_count += 1
    return deleted_count


def delete_temp_media_file(path: Path | str | None) -> None:
    """Удалить временный media-файл, если он существует."""
    if path is None:
        return
    Path(path).unlink(missing_ok=True)


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
            return await self.bot.get_file(file_id)
        except Exception as e:
            logger.exception("Error getting Telegram file info")
            raise ImageUploadError(
                "Не удалось загрузить изображение. Отправьте другое и попробуйте снова.",
                log_message=f"Telegram get_file failed: {type(e).__name__}",
            ) from e

    async def download_file(self, file_id: str, destination_path: str) -> bool:
        """Скачать файл."""
        try:
            file = await self.get_file_info(file_id)
            await self.bot.download_file(file.file_path, destination_path)
            return True
        except Exception as e:
            logger.exception("Error downloading Telegram file")
            return False

    async def download_temp_file_and_get_public_url(self, file_id: str) -> TemporaryTelegramMedia:
        """Скачать Telegram-файл во временную директорию и вернуть локальный путь и публичный URL."""
        ensure_public_base_url()
        file = await self.get_file_info(file_id)
        media_dir = ensure_temp_media_dir()
        filename = f"{uuid.uuid4().hex}{_detect_file_suffix(file)}"
        destination = media_dir / filename
        try:
            await self.bot.download_file(file.file_path, destination=str(destination))
        except Exception as e:
            logger.exception("Error saving Telegram file locally")
            raise ImageUploadError(
                "Не удалось загрузить изображение. Отправьте другое и попробуйте снова.",
                log_message=f"Telegram file download failed: {type(e).__name__}",
            ) from e
        if not destination.exists():
            logger.error(
                "Telegram temp media missing after download",
                extra={
                    "media_filename": filename,
                    "public_url_path": build_public_media_path(filename),
                    "local_path_exists": False,
                    "file_size_bytes": 0,
                },
            )
            raise ImageUploadError(
                "Не удалось загрузить изображение. Отправьте другое и попробуйте снова.",
                log_message="Telegram file missing after local save",
            )
        file_size_bytes = destination.stat().st_size
        logger.info(
            "Telegram temp media saved",
            extra={
                "media_filename": filename,
                "public_url_path": build_public_media_path(filename),
                "local_path_exists": True,
                "file_size_bytes": file_size_bytes,
            },
        )
        return TemporaryTelegramMedia(
            local_path=destination,
            public_url=build_public_media_url(filename),
        )
