"""插件清单（manifest）Pydantic 数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CapabilityType(str, Enum):
    """插件能力类型。"""
    OUTPUT_SINK = "output_sink"
    INBOUND_ROUTER = "inbound_router"


class Capability(BaseModel):
    """单个能力声明。"""
    type: CapabilityType
    id: str
    priority: int = 0
    config_schema: dict[str, Any] = Field(default_factory=dict)
    queue_policy: dict[str, Any] = Field(default_factory=dict)


class Manifest(BaseModel):
    """插件清单。

    每个插件目录下 manifest.json 反序列化为本对象。
    """
    id: str
    name: str
    version: str
    aether_api_version: str
    entry: str = "plugin.py"
    description: str = ""
    author: str = ""
    depends_on: list[str] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)
    # 声明需要的凭证类型（宿主统一注入，解耦具体插件名）
    # 可选值: "ha_url", "ha_token" 等；宿主按声明映射到环境变量
    secrets: list[str] = Field(default_factory=list)

    def has_capability(self, cap_type: CapabilityType) -> bool:
        return any(c.type == cap_type for c in self.capabilities)
