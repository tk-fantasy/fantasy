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


class UIContribution(BaseModel):
    """插件声明的前端 UI 贡献（不写 Vue 代码，只声明意图）。

    Aether 通用渲染器按 type 渲染对应组件，state/action 走通用路由。
    没插件 = 没 ui_contribution = 前端无该 UI → 八竿子打不着。
    """
    slot: str  # 槽位标识（如 chat_input_toolbar）
    type: str  # 预定义类型: toggle_button | icon_button | status_badge
    props: dict[str, Any] = Field(default_factory=dict)
    state_key: str = ""  # 状态读取 key（GET /api/integrations/state/{key}）
    action: str = ""    # 点击触发的 action（POST /api/integrations/action/{name}）


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
    # 前端 UI 贡献声明（不写 Vue 代码，只声明意图，Aether 通用渲染器渲染）
    ui_contributions: list[UIContribution] = Field(default_factory=list)

    def has_capability(self, cap_type: CapabilityType) -> bool:
        return any(c.type == cap_type for c in self.capabilities)
