"""Сервис коротких временных ссылок для загрузок из Cloudflare R2."""
from __future__ import annotations

import asyncio
import secrets
from html import escape
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aiohttp import web

from app.config import settings
from app.db import DownloadLinkRepository, db_manager
from app.services.r2_storage import R2StorageService
from app.utils import logger


DOWNLOAD_ROUTE_PREFIX = "/d"


def build_short_download_path(token: str) -> str:
    """Собрать короткий public path для скачивания."""
    return f"{DOWNLOAD_ROUTE_PREFIX}/{token}"


def build_short_download_url(token: str) -> str:
    """Собрать короткий публичный URL для скачивания."""
    base_url = settings.public_base_url.strip().rstrip("/")
    if not base_url:
        raise RuntimeError("PUBLIC_BASE_URL должен быть задан для коротких download-ссылок")
    return f"{base_url}{build_short_download_path(token)}"


def build_download_redirect_path(token: str) -> str:
    """Собрать path для прямого скачивания файла."""
    return f"{build_short_download_path(token)}/download"


def generate_download_token() -> str:
    """Сгенерировать URL-safe токен достаточной длины."""
    return secrets.token_urlsafe(32)


def get_download_link_expiry() -> datetime:
    """Вычислить время истечения короткой ссылки."""
    return datetime.now(timezone.utc) + timedelta(seconds=settings.r2_signed_url_ttl_seconds)


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_expiry(value: datetime) -> str:
        normalized = _normalize_timestamp(value)
        return normalized.strftime("%d.%m.%Y %H:%M UTC")


def _format_file_size(file_size_bytes: Optional[int]) -> Optional[str]:
        if file_size_bytes is None:
                return None
        if file_size_bytes < 1024:
                return f"{file_size_bytes} B"
        if file_size_bytes < 1024 * 1024:
                return f"{file_size_bytes / 1024:.1f} KB"
        return f"{file_size_bytes / (1024 * 1024):.1f} MB"


def render_download_page(
        *,
        title: str,
        message: str,
        status: int,
        filename: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        download_path: Optional[str] = None,
) -> web.Response:
        """Отрендерить HTML-страницу загрузки или ошибки."""
        details: list[str] = []
        if filename:
                details.append(f"<div class=\"meta-row\"><span>Имя файла</span><strong>{escape(filename)}</strong></div>")
        formatted_size = _format_file_size(file_size_bytes)
        if formatted_size:
                details.append(f"<div class=\"meta-row\"><span>Размер</span><strong>{escape(formatted_size)}</strong></div>")
        if expires_at is not None:
                details.append(
                        f"<div class=\"meta-row\"><span>Ссылка действует до</span><strong>{escape(_format_expiry(expires_at))}</strong></div>"
                )

        button_html = ""
        if download_path:
                button_html = (
                        f'<a class="download-button" href="{escape(download_path, quote=True)}">Скачать файл</a>'
                )

        html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <style>
        :root {{
            color-scheme: light;
            --bg-top: #f7fbff;
            --bg-bottom: #e9f0f7;
            --card-bg: rgba(255, 255, 255, 0.92);
            --card-border: rgba(15, 23, 42, 0.08);
            --text-main: #102033;
            --text-muted: #536275;
            --accent: #0f766e;
            --accent-dark: #0b5a54;
            --shadow: 0 24px 60px rgba(15, 23, 42, 0.12);
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--text-main);
            background:
                radial-gradient(circle at top left, rgba(15, 118, 110, 0.18), transparent 28%),
                linear-gradient(180deg, var(--bg-top), var(--bg-bottom));
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
        }}
        .card {{
            width: min(100%, 640px);
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 24px;
            padding: 32px;
            box-shadow: var(--shadow);
            backdrop-filter: blur(12px);
        }}
        .eyebrow {{
            display: inline-flex;
            margin-bottom: 14px;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(15, 118, 110, 0.12);
            color: var(--accent-dark);
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 0.03em;
            text-transform: uppercase;
        }}
        h1 {{ margin: 0 0 14px; font-size: clamp(28px, 5vw, 40px); line-height: 1.05; }}
        p {{ margin: 0; font-size: 17px; line-height: 1.6; color: var(--text-muted); }}
        .meta {{ margin-top: 28px; padding: 18px 20px; border-radius: 18px; background: rgba(255,255,255,0.78); }}
        .meta-row {{ display: flex; justify-content: space-between; gap: 16px; padding: 10px 0; border-bottom: 1px solid rgba(15, 23, 42, 0.08); }}
        .meta-row:last-child {{ border-bottom: 0; }}
        .meta-row span {{ color: var(--text-muted); }}
        .download-button {{
            display: inline-flex;
            justify-content: center;
            align-items: center;
            width: 100%;
            margin-top: 28px;
            padding: 18px 22px;
            border-radius: 18px;
            background: linear-gradient(135deg, var(--accent), #159f94);
            color: #fff;
            text-decoration: none;
            font-weight: 800;
            font-size: 19px;
            box-shadow: 0 18px 38px rgba(15, 118, 110, 0.28);
        }}
        footer {{ margin-top: 24px; font-size: 14px; line-height: 1.5; color: var(--text-muted); }}
        @media (max-width: 640px) {{
            .card {{ padding: 24px; border-radius: 20px; }}
            .meta-row {{ flex-direction: column; align-items: flex-start; gap: 6px; }}
            .download-button {{ font-size: 18px; }}
        }}
    </style>
</head>
<body>
    <main class="card">
        <div class="eyebrow">Безопасная загрузка</div>
        <h1>{escape(title)}</h1>
        <p>{escape(message)}</p>
        {'<section class="meta">' + ''.join(details) + '</section>' if details else ''}
        {button_html}
        <footer>Ссылка временная. Не передавайте её третьим лицам.</footer>
    </main>
</body>
</html>
"""
        return web.Response(text=html, content_type="text/html", status=status)


class DownloadLinkService:
    """Сервис создания коротких временных ссылок и redirect в R2."""

    def __init__(self, session_factory: Optional[Any] = None, r2_storage: Optional[R2StorageService] = None):
        self._session_factory = session_factory or db_manager.session_factory
        self._r2_storage = r2_storage or R2StorageService()

    async def create_short_download_url(
        self,
        r2_object_key: str,
        *,
        filename: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        content_type: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> str:
        expiry = _normalize_timestamp(expires_at or get_download_link_expiry())
        for _ in range(5):
            token = generate_download_token()
            async with self._session_factory() as session:
                repository = DownloadLinkRepository(session)
                if await repository.get_by_token(token) is not None:
                    continue
                await repository.create_download_link(
                    token=token,
                    r2_object_key=r2_object_key,
                    filename=filename,
                    file_size_bytes=file_size_bytes,
                    content_type=content_type,
                    expires_at=expiry,
                )
            logger.info(
                {
                    "action": "short_download_link_created",
                    "delivery_method": "r2",
                    "status": "success",
                }
            )
            return build_short_download_url(token)
        raise RuntimeError("Failed to generate a unique short download token")

    async def get_link_by_token(self, token: str):
        async with self._session_factory() as session:
            repository = DownloadLinkRepository(session)
            return await repository.get_by_token(token)

    async def resolve_redirect_url(self, token: str) -> str:
        async with self._session_factory() as session:
            repository = DownloadLinkRepository(session)
            link = await repository.get_by_token(token)
            if link is None:
                raise web.HTTPNotFound(text="Download link not found")

            if _normalize_timestamp(link.expires_at) <= datetime.now(timezone.utc):
                raise web.HTTPGone(text="Download link expired")

            signed_url = await asyncio.to_thread(self._r2_storage.generate_signed_url, link.r2_object_key)
            if not signed_url or not signed_url.strip():
                raise web.HTTPInternalServerError(text="Failed to generate download URL")

            await repository.increment_used_count(link.id)

        logger.info(
            {
                "action": "download_link_redirected",
                "delivery_method": "r2",
                "status": "success",
            }
        )
        return signed_url

    async def delete_expired_download_links(self) -> int:
        async with self._session_factory() as session:
            repository = DownloadLinkRepository(session)
            return await repository.delete_expired_download_links()


DOWNLOAD_LINK_SERVICE_APP_KEY = web.AppKey("download_link_service", DownloadLinkService)


async def handle_download_landing(request: web.Request) -> web.StreamResponse:
    """Показать страницу загрузки для короткой ссылки."""
    service = request.app[DOWNLOAD_LINK_SERVICE_APP_KEY]
    token = request.match_info["token"]
    link = await service.get_link_by_token(token)
    if link is None:
        return render_download_page(
            title="Файл не найден",
            message="Ссылка недействительна или файл уже недоступен.",
            status=404,
        )

    if _normalize_timestamp(link.expires_at) <= datetime.now(timezone.utc):
        return render_download_page(
            title="Ссылка истекла",
            message="Срок действия ссылки истёк. Вернитесь в Telegram и создайте новую ссылку.",
            status=410,
        )

    return render_download_page(
        title="Ваш файл готов",
        message="Файл временно хранится в защищённом Cloudflare R2.",
        status=200,
        filename=link.filename,
        file_size_bytes=link.file_size_bytes,
        expires_at=link.expires_at,
        download_path=build_download_redirect_path(token),
    )


async def handle_download_redirect(request: web.Request) -> web.StreamResponse:
    """Сгенерировать свежий signed URL и перенаправить на скачивание файла."""
    service = request.app[DOWNLOAD_LINK_SERVICE_APP_KEY]
    token = request.match_info["token"]
    try:
        signed_url = await service.resolve_redirect_url(token)
    except web.HTTPNotFound:
        return render_download_page(
            title="Файл не найден",
            message="Ссылка недействительна или файл уже недоступен.",
            status=404,
        )
    except web.HTTPGone:
        return render_download_page(
            title="Ссылка истекла",
            message="Срок действия ссылки истёк. Вернитесь в Telegram и создайте новую ссылку.",
            status=410,
        )
    raise web.HTTPFound(location=signed_url)