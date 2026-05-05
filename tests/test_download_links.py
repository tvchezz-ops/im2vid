from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.db.base import Base
from app.db.models import DownloadLink
from app.db.repositories import DownloadLinkRepository
from app.services.download_links import (
    DOWNLOAD_LINK_SERVICE_APP_KEY,
    DOWNLOAD_ROUTE_PREFIX,
    DownloadLinkService,
    build_short_download_url,
    generate_download_token,
    handle_download_landing,
    handle_download_redirect,
)


class FakeR2StorageService:
    def __init__(self, signed_url: str = "https://r2.example.com/signed"):
        self.signed_url = signed_url
        self.calls: list[str] = []

    def generate_signed_url(self, object_name: str) -> str:
        self.calls.append(object_name)
        return self.signed_url


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    db_path = tmp_path / "download-links.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield session_maker

    await engine.dispose()


def test_generate_download_token_is_url_safe_and_long_enough() -> None:
    token = generate_download_token()

    assert len(token) >= 32
    assert "/" not in token
    assert "+" not in token
    assert build_short_download_url(token) == f"https://example.com/d/{token}"


@pytest.mark.asyncio
async def test_create_short_download_url_persists_token_and_object_key(session_factory) -> None:
    service = DownloadLinkService(session_factory=session_factory, r2_storage=FakeR2StorageService())

    short_url = await service.create_short_download_url(
        "temporary-outputs/run/file.mp4",
        filename="imai-test.mp4",
        file_size_bytes=123456,
        content_type="video/mp4",
    )
    token = short_url.rsplit("/", 1)[-1]

    async with session_factory() as session:
        result = await session.execute(select(DownloadLink).where(DownloadLink.token == token))
        link = result.scalar_one()

    assert short_url == f"https://example.com/d/{token}"
    assert link.r2_object_key == "temporary-outputs/run/file.mp4"
    assert link.filename == "imai-test.mp4"
    assert link.file_size_bytes == 123456
    assert link.content_type == "video/mp4"
    assert link.used_count == 0


@pytest.mark.asyncio
async def test_resolve_redirect_url_returns_fresh_signed_url_and_increments_count(session_factory) -> None:
    fake_r2 = FakeR2StorageService("https://r2.example.com/fresh")
    service = DownloadLinkService(session_factory=session_factory, r2_storage=fake_r2)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    short_url = await service.create_short_download_url("temporary-outputs/run/file.mp4", expires_at=expires_at)
    token = short_url.rsplit("/", 1)[-1]

    redirect_url = await service.resolve_redirect_url(token)

    assert redirect_url == "https://r2.example.com/fresh"
    assert fake_r2.calls == ["temporary-outputs/run/file.mp4"]

    async with session_factory() as session:
        repository = DownloadLinkRepository(session)
        link = await repository.get_by_token(token)

    assert link is not None
    assert link.used_count == 1


@pytest.mark.asyncio
async def test_resolve_redirect_url_returns_404_for_unknown_token(session_factory) -> None:
    service = DownloadLinkService(session_factory=session_factory, r2_storage=FakeR2StorageService())

    with pytest.raises(web.HTTPNotFound):
        await service.resolve_redirect_url("missing-token")


@pytest.mark.asyncio
async def test_resolve_redirect_url_returns_410_for_expired_token(session_factory) -> None:
    service = DownloadLinkService(session_factory=session_factory, r2_storage=FakeR2StorageService())
    short_url = await service.create_short_download_url(
        "temporary-outputs/run/file.mp4",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    token = short_url.rsplit("/", 1)[-1]

    with pytest.raises(web.HTTPGone):
        await service.resolve_redirect_url(token)


@pytest.mark.asyncio
async def test_delete_expired_download_links_removes_only_expired_records(session_factory) -> None:
    service = DownloadLinkService(session_factory=session_factory, r2_storage=FakeR2StorageService())
    await service.create_short_download_url(
        "temporary-outputs/run/expired.mp4",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    valid_url = await service.create_short_download_url(
        "temporary-outputs/run/valid.mp4",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    deleted_count = await service.delete_expired_download_links()

    assert deleted_count == 1

    async with session_factory() as session:
        result = await session.execute(select(DownloadLink))
        links = result.scalars().all()

    assert len(links) == 1
    assert links[0].token == valid_url.rsplit("/", 1)[-1]


@pytest_asyncio.fixture
async def download_client(session_factory):
    service = DownloadLinkService(session_factory=session_factory, r2_storage=FakeR2StorageService())
    app = web.Application()
    app[DOWNLOAD_LINK_SERVICE_APP_KEY] = service
    app.router.add_get(f"{DOWNLOAD_ROUTE_PREFIX}/{{token}}", handle_download_landing)
    app.router.add_get(f"{DOWNLOAD_ROUTE_PREFIX}/{{token}}/download", handle_download_redirect)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        yield client, service
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_download_page_returns_html_for_valid_token(download_client) -> None:
    client, service = download_client
    short_url = await service.create_short_download_url(
        "temporary-outputs/run/file.mp4",
        filename="imai-ready.mp4",
        file_size_bytes=6 * 1024 * 1024,
        content_type="video/mp4",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    token = short_url.rsplit("/", 1)[-1]

    response = await client.get(f"/d/{token}")
    html = await response.text()

    assert response.status == 200
    assert "Ваш файл готов" in html
    assert "Скачать файл" in html
    assert "imai-ready.mp4" in html
    assert "/d/" in html


@pytest.mark.asyncio
async def test_download_route_redirects_for_valid_token(download_client) -> None:
    client, service = download_client
    short_url = await service.create_short_download_url(
        "temporary-outputs/run/file.mp4",
        filename="imai-ready.mp4",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    token = short_url.rsplit("/", 1)[-1]

    response = await client.get(f"/d/{token}/download", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == "https://r2.example.com/signed"


@pytest.mark.asyncio
async def test_expired_token_does_not_download(download_client) -> None:
    client, service = download_client
    short_url = await service.create_short_download_url(
        "temporary-outputs/run/file.mp4",
        filename="imai-expired.mp4",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    token = short_url.rsplit("/", 1)[-1]

    response = await client.get(f"/d/{token}/download", allow_redirects=False)
    html = await response.text()

    assert response.status == 410
    assert "Срок действия ссылки истёк" in html


@pytest.mark.asyncio
async def test_missing_token_returns_html_404(download_client) -> None:
    client, _ = download_client

    response = await client.get("/d/missing-token")
    html = await response.text()

    assert response.status == 404
    assert "Файл не найден" in html
    assert "text/html" in response.headers["Content-Type"]