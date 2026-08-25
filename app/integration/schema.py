"""插件清单（manifest）Pydantic 数据模型。"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CapabilityType(str, Enum):
    """插件能力类型。"""
    OUTPUT_SINK = "output_sink"
    INBOUND_ROUTER = "inbound_router"
    # 进程内能力：宿主 import 插件的 adapters.py 注册行为（无子进程）。
    # 见 app/agents/model_family_adapters.py 的发现与加载逻辑。
    MODEL_ADAPTER = "model_adapter"


# 需要 Supervisor 子进程宿主的能力类型。
# 未列出的能力（如 model_adapter）为进程内加载，禁止 spawn 占位 entry。
PROCESS_CAPABILITIES = frozenset({
    CapabilityType.OUTPUT_SINK,
    CapabilityType.INBOUND_ROUTER,
})


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
    # 反向 RPC（方向 2）权限白名单：插件只能调声明了权限的宿主能力。
    # 支持值: "ha"（ha.call_service/get_states/get_devices_grouped）、
    #         "llm"（llm.chat）、"broadcast"（sink.broadcast）。HostMethodRegistry 校验。
    permissions: list[str] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)
    # 声明需要的凭证类型（宿主统一注入，解耦具体插件名）
    # 可选值: "ha_url", "ha_token" 等；宿主按声明映射到环境变量
    secrets: list[str] = Field(default_factory=list)
    # 前端 UI 贡献声明（不写 Vue 代码，只声明意图，Aether 通用渲染器渲染）
    ui_contributions: list[UIContribution] = Field(default_factory=list)

    def has_capability(self, cap_type: CapabilityType) -> bool:
        return any(c.type == cap_type for c in self.capabilities)

    @property
    def needs_subprocess(self) -> bool:
        """是否需要 Supervisor 拉起子进程。

        进程内能力（如 model_adapter）由宿主在本进程内加载，
        entry 只是上传校验要求的占位文件，不能 spawn。
        未声明任何能力的插件（纯反向 RPC 客户端型/测试桩）维持
        子进程宿主——无能力声明 ≠ 进程内能力。
        """
        if not self.capabilities:
            return True
        return any(c.type in PROCESS_CAPABILITIES for c in self.capabilities)
