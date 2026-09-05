"""ValidatorAgent 的 metrics 记录失败兜底分支（446-447 / 462-463）。

metrics_service 挂掉不应影响核查结果本身：成功路径吞掉记录异常继续解析；
LLM 失败路径连错误记录也失败时仍回退"不重试"。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.agents.validator_agent import ValidatorAgent


def _container_with_broken_metrics(monkeypatch):
    import app.container as container_mod

    def boom(*args, **kwargs):
        raise RuntimeError("metrics down")

    monkeypatch.setattr(container_mod, "get_container", lambda: SimpleNamespace(
        metrics_service=SimpleNamespace(record_llm_call=boom)))
    return container_mod


async def test_metrics_failure_on_success_path_is_swallowed(monkeypatch):
    """LLM 正常返回但 metrics 记录失败 → 吞掉异常，照常解析判定。"""
    _container_with_broken_metrics(monkeypatch)
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=SimpleNamespace(
        content=' {"need_retry": false} '))
    v = ValidatorAgent(llm=llm)
    assert await v._llm_semantic_check("已经关闭了客厅灯", "") is False
    llm.ainvoke.assert_awaited_once()


async def test_metrics_failure_on_error_path_still_returns_false(monkeypatch):
    """LLM 抛异常且错误记录也失败 → 双重吞掉，回退"不重试"。"""
    _container_with_broken_metrics(monkeypatch)
    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=RuntimeError("llm down"))
    v = ValidatorAgent(llm=llm)
    assert await v._llm_semantic_check("我会去关闭灯", "") is False
