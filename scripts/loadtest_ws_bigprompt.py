"""Aether WebSocket 压测（放大单轮负载版）。

与 loadtest_ws.py 相同架构，但每轮 query 附带 ~20k 字符填充文本
（模拟 OpenClaw 那 19k tokens 的单轮系统负载），并每轮使用全新
session_id（stateless），避免触发 Aether 历史摘要机制。

用法（conda yolo 环境）:
    python scripts/loadtest_ws_bigprompt.py --stages 20,40,160 --duration 40
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
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

RESP_TIMEOUT = 60.0   # 单轮等待回包上限
SEND_INTERVAL = 1.0   # 轮间间隔
PAD_UNIT = "负载测试上下文填充，模拟长对话历史与知识内容。"
PAD_DEFAULT_CHARS = 20000


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
        "username": f"loadtest-{user_id[:8]}",
        "type": "access",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


PAD_ALPHABET = "负载测试上下文填充模拟长对话历史知识内容。，；：！？的了一是不在人有多他"


def build_pad(chars: int) -> str:
    if chars <= 0:
        return ""
    import random

    random.seed(42)
    return "".join(random.choice(PAD_ALPHABET) for _ in range(chars))


class Worker:
    """一个压测连接：持续发消息直到 stop。"""

    def __init__(self, idx: int, token: str, stop: asyncio.Event, pad: str):
        self.idx = idx
        self.token = token
        self.stop = stop
        self.pad = pad
        self.rounds = 0
        self.ok = 0
        self.timeout = 0
        self.ws_errors = 0
        self.times: list[float] = []

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                async with websockets.connect(WS_URL + f"?token={self.token}",
                                              ping_interval=30, ping_timeout=30,
                                              open_timeout=10, max_size=2 ** 22) as ws:
                    while not self.stop.is_set():
                        session_id = str(uuid.uuid4())  # stateless：每轮新会话
                        t0 = time.perf_counter()
                        await ws.send(json.dumps({
                            "type": "chat",
                            "query": self.pad + "\n回复'收到'两个字",
                            "session_id": session_id,
                        }))
                        try:
                            while True:
                                msg = await asyncio.wait_for(ws.recv(), timeout=RESP_TIMEOUT)
                                data = json.loads(msg)
                                if data.get("type") == "ping":
                                    continue
                                name = (data.get("header") or {}).get("name", "")
                                if name == "Finish":
                                    break
                        except asyncio.TimeoutError:
                            self.timeout += 1
                            break
                        self.rounds += 1
                        self.ok += 1
                        self.times.append(time.perf_counter() - t0)
                        if self.stop.is_set():
                            break
                        await asyncio.sleep(SEND_INTERVAL)
            except asyncio.TimeoutError:
                self.ws_errors += 1
            except Exception:
                self.ws_errors += 1
            if not self.stop.is_set():
                await asyncio.sleep(2.0)


async def check_health(token: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{API_URL}/api/health", headers={"Authorization": f"Bearer {token}"})
            return r.status_code == 200
    except Exception:
        return False


async def monitor_container() -> None:
    while True:
        try:
            out = subprocess.run(
                ["docker", "stats", "aether", "--no-stream", "--format",
                 "{{.CPUPerc}} {{.MemUsage}}"],
                capture_output=True, text=True, timeout=10,
            )
            line = out.stdout.strip()
            if line:
                cpu, mem = line.split(" ", 1)
                print(f"[{time.strftime('%H:%M:%S')}] docker stats: CPU={cpu} MEM={mem}")
        except Exception:
            pass
        await asyncio.sleep(10)


async def run_stage(n: int, duration: float, secret: str, stage_no: int, pad: str) -> bool:
    stop = asyncio.Event()
    token = make_token(secret, str(uuid.uuid4()))
    workers = [Worker(i, make_token(secret, str(uuid.uuid4())), stop, pad) for i in range(n)]

    print(f"\n===== 阶段 {stage_no}: {n} 个并发连接（持续 {duration:.0f}s，填充 {len(pad)} chars）=====")
    tasks = [asyncio.create_task(w.run()) for w in workers]

    t_start = time.time()
    while time.time() - t_start < duration:
        await asyncio.sleep(5)
        ok = sum(w.ok for w in workers)
        to = sum(w.timeout for w in workers)
        we = sum(w.ws_errors for w in workers)
        healthy = await check_health(token)
        print(f"  [{time.strftime('%H:%M:%S')}] 已跑 {time.time()-t_start:.0f}s | "
              f"成功 {ok} | 超时 {to} | 连接错误 {we} | health={'OK' if healthy else 'DOWN'}")

    stop.set()
    await asyncio.sleep(1.0)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    agg_ok = sum(w.ok for w in workers)
    agg_to = sum(w.timeout for w in workers)
    agg_we = sum(w.ws_errors for w in workers)
    all_times = [t for w in workers for t in w.times]
    print(f"--- 阶段 {stage_no} 汇总 ---")
    print(f"  消息轮次: 成功 {agg_ok}, 超时 {agg_to}, 连接/WS错误 {agg_we}, "
          f"成功率 {100 * agg_ok / max(1, agg_ok + agg_to + agg_we):.1f}%")
    if all_times:
        q = statistics.quantiles(all_times, n=20)[18] if len(all_times) >= 20 else max(all_times)
        print(f"  响应延迟: avg {statistics.mean(all_times):.2f}s, p95 {q:.2f}s, max {max(all_times):.2f}s")

    healthy = await check_health(token)
    if not healthy:
        print("  !!! 服务 health 检查失败 — 疑似已崩溃")
        return False
    return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stages", default="20,40,160", help="每阶段并发数，逗号分隔")
    parser.add_argument("--duration", type=float, default=40, help="每阶段持续时间(秒)")
    parser.add_argument("--pad-chars", type=int, default=PAD_DEFAULT_CHARS, help="每轮填充字符数")
    args = parser.parse_args()
    stages = [int(x) for x in args.stages.split(",") if x.strip()]
    pad = build_pad(args.pad_chars)

    secret = load_secret()
    token = make_token(secret, str(uuid.uuid4()))
    print(f"JWT_SECRET 已加载（{len(secret)} chars），目标 {WS_URL}，填充 {len(pad)} chars/轮")
    print(f"初始 health 检查: {'OK' if await check_health(token) else 'FAILED'}")

    mon = asyncio.create_task(monitor_container())
    try:
        for i, n in enumerate(stages, 1):
            ok = await run_stage(n, args.duration, secret, i, pad)
            if not ok:
                print(f"压测终止于阶段 {i}（{n} 并发）：服务不可用")
                break
    finally:
        mon.cancel()


if __name__ == "__main__":
    asyncio.run(main())
