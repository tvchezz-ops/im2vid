"""Database URL selection and endpoint diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./bot.db"


@dataclass(frozen=True)
class DatabaseUrlSelection:
    url: str
    source: str


def normalize_database_url(database_url: str) -> str:
    normalized_url = database_url.strip()
    if normalized_url.startswith("postgres://"):
        return normalized_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if normalized_url.startswith("postgresql://"):
        return normalized_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return normalized_url


def database_url_host(database_url: str) -> str:
    normalized_url = normalize_database_url(database_url)
    if normalized_url.startswith("sqlite"):
        return "local"
    return (urlsplit(normalized_url).hostname or "").lower()


def is_private_database_endpoint(database_url: str) -> bool:
    host = database_url_host(database_url)
    return host.endswith(".railway.internal") or host == "railway.internal"


def is_public_database_endpoint(database_url: str) -> bool:
    host = database_url_host(database_url)
    if not host or host == "local":
        return False
    return (
        host.endswith(".proxy.rlwy.net")
        or host.endswith(".railway.app")
        or host.endswith(".up.railway.app")
        or "tcp.railway" in host
    )


def database_connection_mode(database_url: str) -> str:
    normalized_url = normalize_database_url(database_url)
    if normalized_url.startswith("sqlite"):
        return "local"
    if is_private_database_endpoint(normalized_url):
        return "private"
    if is_public_database_endpoint(normalized_url):
        return "public"
    return "external"


def is_production_env(env: str) -> bool:
    return env.strip().lower() in {"prod", "production"}


def resolve_database_url(
    *,
    database_url: str = "",
    database_private_url: str = "",
    database_public_url: str = "",
    public_fallback_enabled: bool = False,
) -> DatabaseUrlSelection:
    explicit_url = database_url.strip()
    if explicit_url:
        return DatabaseUrlSelection(url=explicit_url, source="DATABASE_URL")

    private_url = database_private_url.strip()
    if private_url:
        return DatabaseUrlSelection(url=private_url, source="DATABASE_PRIVATE_URL")

    public_url = database_public_url.strip()
    if public_url and public_fallback_enabled:
        return DatabaseUrlSelection(url=public_url, source="DATABASE_PUBLIC_URL")

    return DatabaseUrlSelection(url=DEFAULT_DATABASE_URL, source="default")
