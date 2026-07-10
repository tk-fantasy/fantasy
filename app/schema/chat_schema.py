"""聊天协议 Schema：事件（Event）与指令（Instruction）的信封结构。

每个具体消息类显式声明 NAMESPACE / NAME（协议标识），由 build_header 读取后
填入 Header。前端按 `${namespace}.${name}` 分发（见 ChatView.vue 的 switch），
故这两个字符串是跨进程协议契约，改名需同步前端。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class Header(BaseModel):
    type: str
    namespace: str
    name: str
    timestamp: int
    request_id: str
    session_id: str


class ChatSpec(BaseModel):
    """消息负载基类：子类通过 NAMESPACE / NAME 类变量显式声明协议标识。"""

    NAMESPACE: ClassVar[str] = ""
    NAME: ClassVar[str] = ""

    def build_header(self, msg_type: str, request_id: str | None = None, session_id: str | None = None) -> Header:
        return Header(
            type=msg_type,
            namespace=self.NAMESPACE,
            name=self.NAME,
            timestamp=int(time.time() * 1000),
            request_id=request_id or str(uuid.uuid4()),
            session_id=session_id or str(uuid.uuid4()),
        )


class EventPayload(ChatSpec):
    """事件负载基类（客户端 → 服务端方向）。"""


class InstructionPayload(ChatSpec):
    """指令负载基类（服务端 → 客户端方向）。"""


class Event(BaseModel):
    """事件信封：Header + payload dict。"""

    header: Header
    payload: dict[str, Any]

    @staticmethod
    def build_event(payload: EventPayload, request_id: str | None = None, session_id: str | None = None) -> "Event":
        return Event(
            header=payload.build_header("event", request_id, session_id),
            payload=payload.model_dump(),
        )


class Instruction(BaseModel):
    """指令信封：Header + payload dict。"""

    header: Header
    payload: dict[str, Any]

    @staticmethod
    def build_instruction(payload: InstructionPayload, request_id: str, session_id: str) -> "Instruction":
        return Instruction(
            header=payload.build_header("instruction", request_id, session_id),
            payload=payload.model_dump(),
        )


# ── 事件（客户端 → 服务端）──

class Nlp:
    class Request(EventPayload):
        NAMESPACE: ClassVar[str] = "Nlp"
        NAME: ClassVar[str] = "Request"
        query: str = Field(...)
        mcp_list: list[str] = Field(default_factory=list)


# ── 指令（服务端 → 客户端）──

class Template:
    class ToastStream(InstructionPayload):
        NAMESPACE: ClassVar[str] = "Template"
        NAME: ClassVar[str] = "ToastStream"
        stream: str

    class TokenStream(InstructionPayload):
        NAMESPACE: ClassVar[str] = "Template"
        NAME: ClassVar[str] = "TokenStream"
        token: str
        is_final: bool = False

    class CallTool(InstructionPayload):
        NAMESPACE: ClassVar[str] = "Template"
        NAME: ClassVar[str] = "CallTool"
        id: str
        service_name: str
        tool_name: str
        tool_params: dict[str, Any] | None = None
        friendly_name: str | None = None

    class CallToolResult(InstructionPayload):
        NAMESPACE: ClassVar[str] = "Template"
        NAME: ClassVar[str] = "CallToolResult"
        id: str
        success: bool
        tool_name: str | None = None
        tool_response: dict[str, Any] | None = None
        error_message: str | None = None


class Dialog:
    class Exception(InstructionPayload):
        NAMESPACE: ClassVar[str] = "Dialog"
        NAME: ClassVar[str] = "Exception"
        message: str

    class Finish(InstructionPayload):
        NAMESPACE: ClassVar[str] = "Dialog"
        NAME: ClassVar[str] = "Finish"
        success: bool


class Internal:
    class Dispatcher(InstructionPayload):
        NAMESPACE: ClassVar[str] = "Internal"
        NAME: ClassVar[str] = "Dispatcher"
        current_query: str | None = None
        need_storage_history: bool | None = None


class UI:
    class Status(InstructionPayload):
        """Agent 处理阶段状态提示（仅 WebSocket 实时推送，不存数据库）。"""
        NAMESPACE: ClassVar[str] = "UI"
        NAME: ClassVar[str] = "Status"
        phase: str  # "thinking" | "executing" | "retrying" | "finalizing"
        detail: str = ""  # executing 时跟工具名
