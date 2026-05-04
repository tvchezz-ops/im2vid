"""Инициализация пакета routers."""
from app.bot.routers.errors import router as errors_router
from app.bot.routers.generations import router as generations_router
from app.bot.routers.profile import router as profile_router
from app.bot.routers.shop import router as shop_router
from app.bot.routers.start import router as start_router

__all__ = [
    "errors_router",
    "start_router",
    "profile_router",
    "generations_router",
    "shop_router",
]
