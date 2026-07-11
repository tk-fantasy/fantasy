"""通用 API 请求 Pydantic 模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


# --------------- Auth ---------------

class AuthRegisterRequest(BaseModel):
    """POST /auth/register 请求体。"""
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AuthLoginRequest(BaseModel):
    """POST /auth/login 请求体。"""
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AuthRefreshRequest(BaseModel):
    """POST /auth/refresh 请求体。"""
    refresh_token: str = ""


# --------------- Home Info ---------------

class HomeInfoRequest(BaseModel):
    """POST /home/info 请求体。"""
    home_name: str = ""
    owner_name: str = ""
    province: str = ""
    city: str = ""
    district: str = ""


# --------------- Weather ---------------

class WeatherConfigRequest(BaseModel):
    """天气 API 配置保存请求。"""

    host: str = Field(min_length=1, description="和风天气 API 主机")
    kid: str = Field(min_length=1, description="API Key / Public ID")
    sub: str = Field(min_length=1, description="订阅类型")
    private_key: str = Field(default="", description="私钥（为空则保留原值）")


class UserSwitchRequest(BaseModel):
    """POST /users/switch 请求体。"""
    username: str = Field(min_length=1, description="要切换到的用户名")
    password: str = Field(min_length=1, description="目标用户的密码，用于确认切换权限")


class UserLLMKeysRequest(BaseModel):
    """POST /users/{username}/llm_keys 请求体。"""
    keys: list[dict[str, Any]] = Field(default_factory=list, description="LLM keys 列表")


class UserProvidersRequest(BaseModel):
    """POST /users/{username}/providers 请求体。"""
    providers: dict[str, Any] = Field(default_factory=dict, description="providers 配置")


class LLMKeyRequest(BaseModel):
    """POST /llm_keys — 添加或更新 LLM Key。"""
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    type: str = ""
    api_key: str = ""
    id: str = ""


class LLMSettingsRequest(BaseModel):
    """POST /llm/settings — 应用 LLM 设置。"""
    role: str = ""
    key_id: str = ""
    max_concurrency: int = 8
    thinking: Any | None = None
    multimodal: Any | None = None


class VisionFocusRequest(BaseModel):
    """POST /vision/focus — 设置视觉关注指令。"""
    focus: str = ""


class VisionFocusesCreateRequest(BaseModel):
    """POST /vision/focuses — 新增一条视觉关注。"""
    text: str = Field(min_length=1)


class VisionFocusesUpdateRequest(BaseModel):
    """PUT /vision/focuses/{focus_id} — 更新一条视觉关注。"""
    text: str | None = None
    enabled: bool | None = None


# --------------- 高级配置 ---------------

class ExaConfig(BaseModel):
    """Exa 网页搜索配置。api_key 留空则匿名调用 Exa MCP。"""
    api_key: str = ""


class WebSearchConfig(BaseModel):
    """网页搜索配置段（对应 config.json 的 web_search）。"""
    exa: ExaConfig = ExaConfig()


class VisionConfig(BaseModel):
    """视觉参数配置。"""
    downscale_max_side: int = 448
    jpeg_quality: int = 70
    motion_hash_size: int = 16
    motion_threshold: int = 15
    motion_check_interval_seconds: float = 0.2
    min_infer_interval_seconds: float = 3.0
    max_idle_interval_seconds: float = 60.0
    vision_use_img_count: int = 3
    frame_interval_ms: int = 1000
    # 摄像头源：填 RTSP URL 走网络流，留空走 USB
    rtsp_url: str = ""
    rtsp_username: str = ""
    # 注意：rtsp_password 不在此处（避免明文落 config.json），
    # 走 AdvancedConfigRequest.rtsp_password 顶层字段，由路由写 .env


class RAGConfig(BaseModel):
    """RAG 检索参数配置。"""
    recent_turns: int = 5
    retrieve_top_k: int = 6
    retrieve_top_n: int = 3
    soft_max_turns: int = 12
    hard_max_turns: int = 16
    soft_max_tokens: int = 12000
    hard_max_tokens: int = 16000
    soft_max_chars: int = 24000
    hard_max_chars: int = 32000
    summary_blocks: int = 2


class AdvancedConfigRequest(BaseModel):
    """POST /advanced/config 请求体。"""
    web_search: WebSearchConfig | None = None
    vision: VisionConfig | None = None
    rag: RAGConfig | None = None
    # RTSP 密码：单独走顶层，路由层写 .env（RTSP_PASSWORD 变量），
    # config.json 只存变量名 rtsp_password_env。留空表示不修改。
    rtsp_password: str = ""


# --------------- 语义图参数 ---------------

class SgConfigRequest(BaseModel):
    """POST /sg/config 请求体 — 语义图可编辑参数。

    其余参数（pca_dim / umap_n_components / umap_n_epochs / max_paragraph_chars）
    锁定为默认值，不在此暴露：n_components 固定 3D，其余收益小且易误调。
    """
    threshold: float | None = Field(default=None, ge=0.0, le=1.0,
                                    description="向量相似度阈值，决定送 LLM 分析的文档对数量")
    max_workers: int | None = Field(default=None, ge=1, le=32,
                                    description="LLM 并发线程数")
    umap_n_neighbors: int | None = Field(default=None, ge=2, le=100,
                                         description="UMAP 邻居数，大值偏全局结构，小值偏局部")
    umap_min_dist: float | None = Field(default=None, ge=0.0, le=0.99,
                                        description="UMAP 最小距离，越小同簇点越挤")


# --------------- Emoji 偏好 ---------------

class EmojiPreferenceRequest(BaseModel):
    """保存/更新 emoji 偏好的请求体。"""

    scope: str = Field(..., description="偏好作用域")
    key: str = Field(..., description="偏好键名")
    emoji_char: str = Field(..., description="emoji 字符")

    @field_validator("scope", "key", "emoji_char", mode="before")
    @classmethod
    def _strip_str(cls, v: object) -> str:
        return str(v).strip() if isinstance(v, str) else str(v)


# --------------- Chat ---------------

class ChatRequest(BaseModel):
    """POST /chat 请求体。"""
    request_id: str = ""
    session_id: str = ""
    query: str = Field(min_length=1)


# --------------- Home Assistant ---------------

class HAConfigRequest(BaseModel):
    """POST /ha/config 请求体。"""
    url: str = Field(min_length=1)
    token: str = Field(min_length=1)


class HAServiceCallRequest(BaseModel):
    """POST /ha/call_service 请求体。"""
    domain: str = Field(min_length=1)
    service: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


# --------------- MCP ---------------

class MCPConnectRequest(BaseModel):
    """POST /mcp/servers 请求体。"""
    name: str = Field(min_length=1)
    cmd: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)


# --------------- PTZ 云台 ---------------

class PtzMoveRequest(BaseModel):
    """POST /ptz/move 请求体。direction: up/down/left/right。"""
    direction: str = Field(min_length=1)


class PtzStepRequest(BaseModel):
    """POST /ptz/step 请求体。direction: up/down/left/right。"""
    direction: str = Field(min_length=1)


class PtzConfigRequest(BaseModel):
    """POST /ptz/config 请求体 — PTZ 云台配置。

    密码走顶层 password 字段，路由层写 .env（PTZ_PASSWORD 变量），
    config.json 只存变量名 password_env。留空表示不修改。
    """
    enabled: bool = False
    ip: str = ""
    port: int = Field(default=80, ge=1, le=65535)
    username: str = ""
    password: str = ""
    speed: float = 0.5
    step_ms: int = 300


# --------------- Model Test ---------------

class ModelTestRequest(BaseModel):
    """POST /models/test 请求体。"""
    base_url: str = ""
    model: str = ""
    role: str = "chat"
    api_key: str = ""
    chat_path: str = "/chat/completions"
    embed_path: str = "/v1/embeddings"


# --------------- Rules ---------------

class RuleCreateRequest(BaseModel):
    """POST /task/rule 请求体。"""
    text: str = ""


class RulePayloadRequest(BaseModel):
    """POST /rules 请求体。"""
    condition: str = ""
    actions: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True
    cooldown_seconds: int = 10


class RuleEnabledRequest(BaseModel):
    """POST /rules/{rule_id}/enabled 请求体。"""
    enabled: bool = True


# --------------- Scheduled Tasks ---------------

class ScheduledTaskCreateRequest(BaseModel):
    """POST /scheduled-tasks 请求体。

    schedule 形如 {"kind":"at","at":"2026-07-07T08:00:00"} /
                  {"kind":"every","every_seconds":3600} /
                  {"kind":"cron","expr":"0 8 * * *"}
    payload 形如 {"kind":"tool","tool_name":"ha_devices___call_service","tool_input":{...}} /
                  {"kind":"message","message":"该起床了"}
    """
    name: str = ""
    schedule: dict[str, Any]
    payload: dict[str, Any]
    enabled: bool = True


class ScheduledTaskEnabledRequest(BaseModel):
    """POST /scheduled-tasks/{id}/enabled 请求体。"""
    enabled: bool = True


class ScheduleParseRequest(BaseModel):
    """POST /scheduled-tasks/parse-schedule 请求体。

    phrase 为自然语言时间描述，如「每天早上8点」「明天10点」「每30分钟」。
    """
    phrase: str = Field(min_length=1)


# --------------- Unique Settings ---------------

class UniqueSettingsRequest(BaseModel):
    """POST /unique 请求体。"""
    persona: str = ""
