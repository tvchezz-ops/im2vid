from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.services import telegram_files
from app.utils.errors import ImageUploadError


def test_build_public_media_url_uses_media_prefix() -> None:
    assert telegram_files.build_public_media_url("sample.jpg") == "https://example.com/media/sample.jpg"


def test_create_media_app_serves_temp_media_dir_on_media_route() -> None:
    app = telegram_files.create_media_app()

    resources = list(app.router.resources())
    assert any(
        getattr(resource, "_prefix", None) == "/media"
        and Path(getattr(resource, "_directory", "")) == telegram_files.get_temp_media_dir()
        for resource in resources
    )
    assert any(getattr(resource, "canonical", None) == "/d/{token}" for resource in resources)
    assert any(getattr(resource, "canonical", None) == "/d/{token}/download" for resource in resources)


@pytest.mark.asyncio
async def test_download_temp_file_returns_existing_local_file_and_media_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(telegram_files.settings, "temp_media_dir", str(tmp_path))

    class FakeBot:
        async def get_file(self, file_id: str):
            return SimpleNamespace(file_path="photos/input.jpg")

        async def download_file(self, file_path: str, destination: str):
            Path(destination).write_bytes(b"telegram-image")

    service = telegram_files.TelegramFilesService(FakeBot())

    media = await service.download_temp_file_and_get_public_url("file-id")

    assert media.local_path.exists() is True
    assert media.local_path.stat().st_size == len(b"telegram-image")
    assert media.public_url == f"https://example.com/media/{media.local_path.name}"


@pytest.mark.asyncio
async def test_download_temp_file_raises_when_local_file_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(telegram_files.settings, "temp_media_dir", str(tmp_path))

    class FakeBot:
        async def get_file(self, file_id: str):
            return SimpleNamespace(file_path="photos/input.jpg")

        async def download_file(self, file_path: str, destination: str):
            return None

    service = telegram_files.TelegramFilesService(FakeBot())

    with pytest.raises(ImageUploadError, match="Telegram file missing after local save"):
        await service.download_temp_file_and_get_public_url("file-id")