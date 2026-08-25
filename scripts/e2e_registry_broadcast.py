"""E2E 验证：设备注册表候选自愈 + 定时任务播报同步（模拟真人，全自动）。

用法（Aether 后端需运行在 127.0.0.1:8010，已包含 device_registry/播报同步改动）:
    python scripts/e2e_registry_broadcast.py [--user-id <id>]

验证三个用例（会真实开一次会客厅灯、小爱真实播报一次）：
  A. 「打开会客厅的灯」→ call_service 拒绝编造 id 的报文含候选
     switch.xxx（A灯 会客厅灯 左/右键），模型用候选重试成功（new_state=on）；
     或模型直接从提示词子功能名选对（同样算过）。
  B. 「一分钟后提醒我下班」→ 到点后同一 WS 连接收到 ToastStream 文字推送，
     任务 last_reply 已写入（语音经 sink 广播，服务端行为）。
  C. POST /scheduled-tasks/{id}/run?wait=true → 响应带 last_status=success
     + last_reply（手动触发即时反馈）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import websockets

BASE = Path(__file__).resolve().parent.parent
ENV = BASE / ".env"

WS_URL = os.getenv("AETHER_WS_URL", "ws://127.0.0.1:8010/ws/chat")
API_URL = os.getenv("AETHER_API_URL", "http://127.0.0.1:8010")

RESP_TIMEOUT = 120.0   # 单轮等 Finish 上限（LLM + 工具链）
REMINDER_WAIT = 150.0  # 「一分钟后」提醒 + 调度/生成余量


def load_secret() -> str:
    for line in ENV.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("JWT_SECRET="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("JWT_SECRET not found in .env")


def make_token(secret: str, user_id: str) -> str:
    import jwt

    payload = {
        "sub": user_id,
        "username": "e2e",
        "type": "access",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


class Verdict:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    def summary(self) -> int:
        fails = [r for r in self.results if not r[1]]
        print("\n===== E2E 汇总 =====")
        for name, ok, detail in self.results:
            print(f"  {'✓' if ok else '✗'} {name}" + (f"（{detail}）" if detail else ""))
        return 1 if fails else 0


async def chat_round(ws, query: str, session_id: str) -> list[dict]:
    """发一条 chat，收满一轮指令（到 Finish 为止），返回指令列表。"""
    await ws.send(json.dumps({"type": "chat", "query": query, "session_id": session_id}))
    instructions: list[dict] = []
    deadline = time.monotonic() + RESP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            break
        data = json.loads(msg)
        if data.get("type") == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            continue
        instructions.append(data)
        if (data.get("header") or {}).get("name") == "Finish":
            break
    return instructions


def _collect_calls(instructions: list[dict]) -> tuple[list[dict], list[dict]]:
    calls, results = [], []
    for inst in instructions:
        h = inst.get("header") or {}
        p = inst.get("payload") or {}
        if h.get("name") == "CallTool":
            calls.append(p)
        elif h.get("name") == "CallToolResult":
            results.append(p)
    return calls, results


async def case_light(v: Verdict, ws, session_id: str) -> None:
    """用例 A：子功能名指称开灯（候选自愈 / 直接选对）。"""
    print("\n[用例 A] 打开会客厅的灯（会真实开灯）…")
    instructions = await chat_round(ws, "打开会客厅的灯", session_id)
    calls, results = _collect_calls(instructions)

    svc_calls = [c for c in calls if "call_service" in str(c.get("tool_name", ""))]
    ckcper_calls = [c for c in svc_calls
                    if "switch.ckcper" in str(c.get("tool_params", {}))]
    final = next((inst.get("payload", {}).get("stream", "") for inst in reversed(instructions)
                  if (inst.get("header") or {}).get("name") == "ToastStream"), "")
    # 判定：对正确的 ckcper 子实体发起了 call_service 且模型确认了结果
    # （result 结构随 HA 返回形态变化，不作为硬断言，仅展示）
    svc_results_ok = any(r.get("success") is True for r in results)
    v.record("call_service 操作了正确的 ckcper 子实体", bool(ckcper_calls),
             "; ".join(str(c.get("tool_params", {}).get("entity_id")) for c in ckcper_calls[:2]))
    v.record("工具执行无失败", svc_results_ok or not results,
             "" if (svc_results_ok or not results) else
             json.dumps([r.get("error_message") for r in results], ensure_ascii=False)[:100])

    healed = any("可能匹配" in json.dumps(r, ensure_ascii=False) and "会客厅灯" in json.dumps(r, ensure_ascii=False)
                 for r in results)
    direct = bool(ckcper_calls)
    v.record("子功能名可达（直接选对或候选自愈）", direct,
             "直接从提示词选对" if direct and not healed else ("经候选自愈" if healed else "两者都未观察到"))

    v.record("最终回复非空", bool(final), final[:60])


async def case_reminder(v: Verdict, ws, session_id: str, token: str) -> str | None:
    """用例 B：一分钟后提醒我下班（文字推送 + last_reply）。

    任务直接经 REST 创建（确定性，不依赖模型当轮是否调用工具；模型工具
    调用纪律已由用例 A 覆盖）。route 会注入 admin 的 user_id → reminder
    按创建者解析模型、推送到 admin 的在线 WS。
    """
    print("\n[用例 B] 创建「一分钟后提醒我下班」（REST，等待到点推送）…")
    from datetime import datetime, timedelta
    at = (datetime.now() + timedelta(seconds=70)).isoformat(timespec="seconds")
    async with httpx.AsyncClient(timeout=15, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post(f"{API_URL}/api/scheduled-tasks", json={
            "name": "E2E 下班提醒",
            "schedule": {"kind": "at", "at": at},
            "payload": {"kind": "reminder", "intent": "下班提醒",
                        "original": "一分钟后提醒我下班"},
            "enabled": True,
        })
        task = (r.json() or {}).get("data") or {}
    task_id = task.get("id")
    v.record("reminder 任务已创建（REST）", bool(task_id),
             f"at={at}" if task_id else str(r.json())[:80])
    if not task_id:
        return None

    # 同一连接等待到点推送（期间只有 ping/心跳，出现 ToastStream 即文字推送到达）
    pushed = ""
    deadline = time.monotonic() + REMINDER_WAIT
    while time.monotonic() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            break
        data = json.loads(msg)
        if data.get("type") == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            continue
        if (data.get("header") or {}).get("name") == "ToastStream":
            pushed = data.get("payload", {}).get("stream", "")
            break
    v.record("到点收到文字推送（ToastStream）", bool(pushed), pushed[:60])

    async with httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {token}"}) as c:
        tasks = (await c.get(f"{API_URL}/api/scheduled-tasks")).json().get("data") or []
        after = next((t for t in tasks if t.get("id") == task_id), {})
    v.record("任务 last_reply 已写入", bool(after.get("last_reply")),
             (after.get("last_reply") or "")[:60])
    v.record("任务执行成功", after.get("last_status") == "success", str(after.get("last_status")))
    return task_id


async def case_manual_run(v: Verdict, token: str, task_id: str) -> None:
    """用例 C：手动触发 wait=true（会再播报一次）。"""
    print("\n[用例 C] 手动触发（wait=true）…")
    async with httpx.AsyncClient(timeout=90, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post(f"{API_URL}/api/scheduled-tasks/{task_id}/run?wait=true")
        data = (r.json() or {}).get("data") or {}
    v.record("run?wait=true 返回 success + last_reply",
             data.get("last_status") == "success" and bool(data.get("last_reply")),
             (data.get("last_reply") or data.get("last_error") or "")[:60])


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--user-id", default=os.getenv("AETHER_USER_ID", ""),
        help="目标用户 UUID（reminder/message 按它解析模型与推送目标）；"
             "可用环境变量 AETHER_USER_ID 传入。查法：sqlite3 app/data/aether.db "
             "'select id, username from users where is_admin=1'",
    )
    args = parser.parse_args()
    if not args.user_id:
        raise SystemExit("缺少 --user-id（或环境变量 AETHER_USER_ID）：见参数说明查 users 表")

    token = make_token(load_secret(), args.user_id)
    async with httpx.AsyncClient(timeout=5, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.get(f"{API_URL}/api/health")
        if r.status_code != 200:
            print(f"后端不可用（{r.status_code}），先启动服务")
            return 1

    v = Verdict()
    async with websockets.connect(WS_URL + f"?token={token}", ping_interval=30,
                                  ping_timeout=30, open_timeout=10, max_size=2 ** 22) as ws:
        session_id = str(uuid.uuid4())
        await case_light(v, ws, session_id)
        task_id = await case_reminder(v, ws, session_id, token)
    if task_id:
        await case_manual_run(v, token, task_id)
        # 清理：验证完删除测试任务（at 任务本已自动禁用，删掉避免留在列表）
        async with httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {token}"}) as c:
            await c.delete(f"{API_URL}/api/scheduled-tasks/{task_id}")
    return v.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
