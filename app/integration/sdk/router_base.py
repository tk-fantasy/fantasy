"""InboundRouter 抽象基类 —— 插件入站路由能力实现此接口。"""

from abc import ABC, abstractmethod


class InboundRouter(ABC):
    """入站路由能力契约。

    用户在 ChatView 切模式后，文字经 InboundRouter 路由到插件处理。
    典型：小爱直通模式——文字原样转小爱原生执行（execute=true），不进 LLM。
    """

    @abstractmethod
    async def route(self, text: str) -> dict:
        """处理入站文字。返回执行结果 dict（至少含 ok 或错误信息）。"""
        ...
