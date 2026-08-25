"""OpenClaw Gateway 聊天压测脚本（OpenAI 兼容 HTTP + SSE 流式）。

用法（conda yolo 环境）:
    python scripts/loadtest_openclaw.py --stages 10,20,40,80,120,160 --duration 40

行为:
- 每阶段保持 N 个并发 HTTP 请求，每个请求 POST /v1/chat/completions
  （stream: true，stateless 会话），收到 [DONE] 后间隔 1s 发下一轮
- 旁路监控 /healthz 判断服务是否存活
- 每阶段输出成功率 / 延迟 / 错误统计
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import time

import httpx

URL = os.getenv("OPENCLAW_URL", "http://127.0.0.1:18789")
TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
CONTAINER = os.getenv("OPENCLAW_CONTAINER", "openclaw-test")

RESP_TIMEOUT = 60.0   # 单轮等待回包上限
SEND_INTERVAL = 1.0   # 轮间间隔


class Worker:
    """一个压测连接：持续发请求直到 stop。"""

    def __init__(self, idx: int, stop: asyncio.Event):
        self.idx = idx
        self.stop = stop
        self.rounds = 0
        self.ok = 0
        self.timeout = 0
        self.http_errors = 0
        self.times: list[float] = []

    async def run(self) -> None:
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "openclaw",
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "stream": True,
        }
        while not self.stop.is_set():
            try:
                async with httpx.AsyncClient(timeout=RESP_TIMEOUT + 5) as c:
                    t0 = time.perf_counter()
                    done = False
                    try:
                        async with c.stream("POST", f"{URL}/v1/chat/completions",
                                            headers=headers, json=body) as r:
                            if r.status_code != 200:
                                self.http_errors += 1
                            else:
                                async for line in r.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    payload = line[5:].strip()
                                    if payload == "[DONE]":
                                        done = True
                                        break
                    except asyncio.TimeoutError:
                        self.timeout += 1
                        continue
                    if not done:
                        self.http_errors += 1
                        continue
                    self.rounds += 1
                    self.ok += 1
                    self.times.append(time.perf_counter() - t0)
                    if self.stop.is_set():
                        break
                    await asyncio.sleep(SEND_INTERVAL)
            except asyncio.TimeoutError:
                self.timeout += 1
            except Exception:
                self.http_errors += 1
            if not self.stop.is_set():
                await asyncio.sleep(1.0)


async def check_health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{URL}/healthz")
            return r.status_code == 200
    except Exception:
        return False


async def monitor_container() -> None:
    while True:
        try:
            out = subprocess.run(
                ["docker", "stats", CONTAINER, "--no-stream", "--format",
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


async def run_stage(n: int, duration: float, stage_no: int) -> bool:
    stop = asyncio.Event()
    workers = [Worker(i, stop) for i in range(n)]

    print(f"\n===== 阶段 {stage_no}: {n} 个并发请求（持续 {duration:.0f}s）=====")
    tasks = [asyncio.create_task(w.run()) for w in workers]

    t_start = time.time()
    while time.time() - t_start < duration:
        await asyncio.sleep(5)
        ok = sum(w.ok for w in workers)
        to = sum(w.timeout for w in workers)
        he = sum(w.http_errors for w in workers)
        healthy = await check_health()
        print(f"  [{time.strftime('%H:%M:%S')}] 已跑 {time.time()-t_start:.0f}s | "
              f"成功 {ok} | 超时 {to} | 错误 {he} | health={'OK' if healthy else 'DOWN'}")

    stop.set()
    await asyncio.sleep(1.0)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    agg_ok = sum(w.ok for w in workers)
    agg_to = sum(w.timeout for w in workers)
    agg_he = sum(w.http_errors for w in workers)
    all_times = [t for w in workers for t in w.times]
    print(f"--- 阶段 {stage_no} 汇总 ---")
    print(f"  消息轮次: 成功 {agg_ok}, 超时 {agg_to}, HTTP/连接错误 {agg_he}, "
          f"成功率 {100 * agg_ok / max(1, agg_ok + agg_to + agg_he):.1f}%")
    if all_times:
        q = statistics.quantiles(all_times, n=20)[18] if len(all_times) >= 20 else max(all_times)
        print(f"  响应延迟: avg {statistics.mean(all_times):.2f}s, p95 {q:.2f}s, max {max(all_times):.2f}s")

    healthy = await check_health()
    if not healthy:
        print("  !!! 服务 health 检查失败 — 疑似已崩溃")
        return False
    return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stages", default="10,20,40,80,120,160", help="每阶段并发数，逗号分隔")
    parser.add_argument("--duration", type=float, default=40, help="每阶段持续时间(秒)")
    args = parser.parse_args()
    stages = [int(x) for x in args.stages.split(",") if x.strip()]

    if not TOKEN:
        raise SystemExit("请先设置 OPENCLAW_GATEWAY_TOKEN 环境变量（网关 Bearer token）")

    print(f"目标 {URL}（容器 {CONTAINER}）")
    print(f"初始 health 检查: {'OK' if await check_health() else 'FAILED'}")

    mon = asyncio.create_task(monitor_container())
    try:
        for i, n in enumerate(stages, 1):
            ok = await run_stage(n, args.duration, i)
            if not ok:
                print(f"压测终止于阶段 {i}（{n} 并发）：服务不可用")
                break
    finally:
        mon.cancel()


if __name__ == "__main__":
    asyncio.run(main())
