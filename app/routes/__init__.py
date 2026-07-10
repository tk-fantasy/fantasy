"""路由模块导出。"""
from .settings_routes import router as settings_router
from .home_routes import router as home_router
from .weather_routes import router as weather_router
from .emoji_routes import router as emoji_router
from .advanced_routes import router as advanced_router
from .stt_routes import router as stt_router

__all__ = ["settings_router", "home_router", "weather_router", "emoji_router", "advanced_router", "stt_router"]
