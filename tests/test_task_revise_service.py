"""app/services/task_revise_service.py 测试：定时任务对话式修改与解释。

LLM 调用发生在函数内（lazy import ChatOpenAI），因此直接把
langchain_openai.ChatOpenAI / 模型配置 / http 客户端全部替换为假件，
不触网。覆盖：空输入守卫、JSON 解析后的字段兜底、change_summary 提取、
schedule 合法性校验传播、explain 的文本路径。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import langchain_openai
import app.agents.langgraph_agent as lga_mod
import app.clients.http_client as http_mod

from app.services.task_revise_service import explain_task, revise_task


_TASK = {
    "id": "t1",
    "name": "起床提醒",
    "schedule": {"kind": "cron", "expr": "0 8 * * *"},
    "payload": {"kind": "reminder", "intent": "提醒起床", "original": "每天8点提醒起床"},
    "enabled": True,  # 运行时噪声字段，不应进 prompt
}


def _install_fake_llm(monkeypatch, *, content=None, exc=None):
    """替换三个 lazy import 目标；返回捕获器 dict（kwargs/messages）。"""
    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        async def ainvoke(self, messages):
            captured["messages"] = messages
            if exc is not None:
                raise exc
            return SimpleNamespace(content=content)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeLLM)
    monkeypatch.setattr(lga_mod, "_load_model_config_from_config",
                        lambda: {"model": "fake-model", "base_url": "http://fake", "api_key": "k"})
    monkeypatch.setattr(http_mod, "new_sync_client", lambda *a, **k: None)
    monkeypatch.setattr(http_mod, "new_client", lambda *a, **k: None)
    return captured


# --------------- revise_task ---------------

async def test_revise_empty_instruction_guard(monkeypatch):
    with pytest.raises(ValueError, match="不能为空"):
        await revise_task(_TASK, "   ")
    # 守卫阶段不应触碰任何配置/客户端


async def test_revise_happy_path_replaces_fields(monkeypatch):
    new_task = {
        "name": "起床提醒",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "payload": {"kind": "reminder", "intent": "提醒起床", "original": "每天9点提醒起床"},
        "change_summary": "触发时间从 8 点改为 9 点",
    }
    captured = _install_fake_llm(
        monkeypatch, content=json.dumps(new_task, ensure_ascii=False)
    )

    result = await revise_task(_TASK, "改成9点")
    assert result["summary"] == "触发时间从 8 点改为 9 点"
    assert result["task"]["schedule"]["expr"] == "0 9 * * *"
    # 顶层只保留任务字段 + summary 已弹出
    assert "change_summary" not in result["task"]

    # prompt：当前时间已注入、原任务 brief 只有三个可改字段
    system_text, user_text = captured["messages"][0][1], captured["messages"][1][1]
    assert "{now}" not in system_text
    brief = json.loads(user_text.split("JSON:\n", 1)[1].split("\n\n修改指令", 1)[0])
    assert brief["name"] == "起床提醒"
    assert "enabled" not in brief  # 运行时噪声被剥离


async def test_revise_missing_fields_fall_back_to_current(monkeypatch):
    llm_reply = {"schedule": {"kind": "every", "every_seconds": 3600}}  # 缺 name/payload/summary
    _install_fake_llm(monkeypatch, content=json.dumps(llm_reply))

    result = await revise_task(_TASK, "改成每小时一次")
    assert result["task"]["name"] == "起床提醒"  # 兜底回填
    assert result["task"]["payload"] == _TASK["payload"]
    assert result["summary"] == "已更新"  # 无 change_summary 时默认文案


async def test_revise_invalid_schedule_propagates_validation(monkeypatch):
    llm_reply = {"schedule": {"kind": "cron", "expr": "不是 cron"}}
    _install_fake_llm(monkeypatch, content=json.dumps(llm_reply))

    with pytest.raises(ValueError):
        await revise_task(_TASK, "随便改")


async def test_revise_llm_failure_propagates(monkeypatch):
    _install_fake_llm(monkeypatch, exc=RuntimeError("连接超时"))
    with pytest.raises(RuntimeError, match="连接超时"):
        await revise_task(_TASK, "改成9点")


# --------------- explain_task ---------------

async def test_explain_empty_question_guard(monkeypatch):
    with pytest.raises(ValueError, match="不能为空"):
        await explain_task(_TASK, "")


async def test_explain_returns_llm_text_stripped(monkeypatch):
    captured = _install_fake_llm(monkeypatch, content="  这个任务是每天 8 点执行。  \n")

    answer = await explain_task(_TASK, "这个任务是执行一次还是每天都跑？")
    assert answer == "这个任务是每天 8 点执行。"
    user_text = captured["messages"][1][1]
    assert "这个任务是执行一次还是每天都跑？" in user_text


async def test_explain_empty_output_falls_back(monkeypatch):
    _install_fake_llm(monkeypatch, content="")
    answer = await explain_task(_TASK, "为什么？")
    assert answer == "无法生成解释。"
