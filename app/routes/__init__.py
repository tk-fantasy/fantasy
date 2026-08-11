"""路由模块导出。"""
from .llm_key_routes import router as llm_key_router
from .global_config_routes import router as global_config_router
from .home_routes import router as home_router
from .weather_routes import router as weather_router
from .emoji_routes import router as emoji_router
from .advanced_routes import router as advanced_router
from .stt_routes import router as stt_router

__all__ = [
    "llm_key_router",
    "global_config_router",
    "home_router",
    "weather_router",
    "emoji_router",
    "advanced_router",
    "stt_router",
]
