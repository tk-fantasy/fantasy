"""OutputSink 抽象基类 —— 插件输出能力实现此接口。"""

from abc import ABC, abstractmethod


class OutputSink(ABC):
    """输出能力契约。

    Aether 的 assistant 回复会广播到所有启用的 sink。
    """

    @abstractmethod
    async def speak(self, text: str, msg_id: str = "") -> dict:
        """播报一段文本。返回执行结果 dict（至少含 ok 或错误信息）。"""
        ...

    @abstractmethod
    async def interrupt(self) -> dict:
        """中断当前播报。返回执行结果 dict。"""
        ...
