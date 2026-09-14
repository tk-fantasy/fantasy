# -*- coding: utf-8 -*-
"""E2E smoke：起完整 app（隔离临时 DB/config），跨重启验证本轮修复的关键行为。"""
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(r"D:\Aether")
sys.path.insert(0, str(ROOT))

tmp = Path(tempfile.mkdtemp(prefix="aether_e2e_"))

# 极简 mock OpenAI：新增 key 时的连接测试用
class _FakeOpenAI(BaseHTTPRequestHandler):
    def do_POST(self):
        # 必须先读完请求体再响应：否则服务端提前关连接，Windows 下客户端
        # 会拿到空消息的 BrokenResourceError（表现为"未知错误: "）
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

mock_srv = HTTPServer(("127.0.0.1", 9099), _FakeOpenAI)
threading.Thread(target=mock_srv.serve_forever, daemon=True).start()

import app.core.config as cfg  # noqa: E402
cfg.CONFIG_PATH = tmp / "config.json"  # 写入隔离；内存 CONFIG 用真实值无妨
import app.core.database as dbmod  # noqa: E402
dbmod.DB_PATH = tmp / "aether.db"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.core import auth as auth_mod  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(detail)[:200]) if not cond else ""), flush=True)
    if not cond:
        failures.append(name)


def wait_ready(c, tries=60):
    last = None
    for _ in range(tries):
        try:
            last = c.get("/docs")
            if last.status_code == 200:
                return True
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5)
    check("server ready", False, last)
    return False


# ---------- 第一阶段：首次启动 ----------
with TestClient(app) as c:
    if wait_ready(c):
        check("health/docs 200", True)

        r = c.post("/api/auth/register", json={"username": "e2e", "password": "pass123456"})
        check("register", r.status_code == 200, r.text)
        old_access = c.cookies.get(auth_mod.ACCESS_COOKIE)

        # ③ llm_keys 写透 config.json（新增 key 会真连测试 → 打 mock OpenAI）
        r = c.post("/api/llm_keys", json={
            "base_url": "http://127.0.0.1:9099/v1", "model": "e2e-model",
            "type": "chat", "api_key": "sk-e2e-secret"})
        check("POST /api/llm_keys", r.status_code == 200, r.text)
        if (tmp / "config.json").exists():
            saved = json.loads((tmp / "config.json").read_text(encoding="utf-8")).get("llm_keys", [])
            check("llm_keys 落盘 config.json", any(k.get("model") == "e2e-model" for k in saved), saved)
            check("落盘已剥明文 api_key", all(not k.get("api_key") for k in saved), saved)
        else:
            check("llm_keys 落盘 config.json", False, "config.json not created at " + str(tmp))

        # ① 已删端点：GET 落到静态挂载返回 404；POST 部分匹配静态挂载返回 405
        for method, path in [("POST", "/api/chat"), ("POST", "/api/models/test"),
                             ("GET", "/api/files/browse"), ("GET", "/api/weather/locate"),
                             ("GET", "/api/users/me"), ("GET", "/api/users/e2e/llm_keys"),
                             ("GET", "/api/users/e2e/providers"), ("POST", "/api/doc/chat")]:
            r = c.request(method, path, json={"message": "x"})
            check(f"已删端点 {method} {path}", r.status_code in (404, 405), r.status_code)

        # ⑥ 提示词（config guidelines 已改写，不再出现 verify_condition）
        r = c.get("/api/unique")
        g = json.dumps(r.json().get("data", {}), ensure_ascii=False)
        check("guidelines 无 verify_condition", "verify_condition" not in g, g[:120])

        # ⑤ MCP 注册表里 verify_condition 已消失
        from app.container import get_container
        names = list(get_container().tool_deps.mcp_client_manager._tools.keys())
        check("MCP 工具表无 verify_condition", all("verify_condition" not in str(n) for n in names), len(names))

        # ④ 登出 → 当前 token 立即失效
        r = c.post("/api/auth/logout")
        check("logout", r.status_code == 200, r.text)
        r = c.get("/api/users")
        check("登出后旧 token 被拒", r.status_code == 401, f"{r.status_code} {r.text[:80]}")

# ---------- 第二阶段：重启（同一份临时 DB/config/密钥） ----------
with TestClient(app) as c:
    if wait_ready(c):
        r = c.post("/api/auth/login", json={"username": "e2e", "password": "pass123456"})
        check("relogin", r.status_code == 200, r.text)
        r = c.get("/api/llm_keys")
        models = [k.get("model") for k in r.json().get("data", [])]
        check("重启后 llm_keys 仍在（不再丢）", "e2e-model" in models, models)

        # 重启后黑名单回灌：登出前签发的旧 access token 依然被拒
        r = c.get("/api/users", cookies={auth_mod.ACCESS_COOKIE: old_access})
        check("重启后已登出 token 仍被拒", r.status_code == 401,
              f"{r.status_code} {r.text[:80]}")

mock_srv.shutdown()
print("===== E2E SMOKE: " + (f"{len(failures)} FAILURES" if failures else "ALL PASS") + " =====")
sys.exit(1 if failures else 0)
