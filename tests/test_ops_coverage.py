"""目标模块补覆盖率测试（app/ops/diagnose、config_probes、ops_routes、pack_export、upgrade、startup_progress）。

边界 mock 原则：只 mock subprocess / HTTP 客户端 / 文件系统（tmp_path）/ socket，
业务逻辑全部走真实代码并断言真实返回值。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import socket
import sqlite3
import subprocess
import sys
import tarfile
import types
import urllib.error
import urllib.request
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

# ==================== 共享小工具 ====================


def _listener() -> tuple[socket.socket, int]:
    """本机临时监听 socket（测试期间保持打开，connect 必成功）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def _dead_port() -> int:
    """分配后立即关闭的端口（connect 必拒绝）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeResp:
    """httpx.Response 替身：status_code / .json() / .text。"""

    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


async def _consume(aiter):
    """消费异步可迭代对象为列表（验证 docker load 流式 content）。"""
    return [chunk async for chunk in aiter]


# ==================== app/startup_progress ====================
# conftest 把单例的 start/stop/mark_ready/set 换成了 lambda；这里用全新的
# _State 实例（类方法未被 patch），可以安全测真实行为，端口用 0（ephemeral）。


class TestStartupProgress:
    def _fresh(self, monkeypatch):
        from app import startup_progress as sp

        monkeypatch.setattr(sp, "_PROGRESS_HOST", "127.0.0.1")
        monkeypatch.setattr(sp, "_PROGRESS_PORT", 0)
        return sp._State()

    def test_state_transitions(self):
        from app.startup_progress import _State

        s = _State()
        first = s.snapshot()
        assert first["ready"] is False and first["stage"] == "正在启动..."
        assert first["elapsed_sec"] >= 0
        s.set("导入模型")
        snap = s.snapshot()
        assert snap["stage"] == "导入模型" and snap["ready"] is False
        s.mark_ready()
        snap = s.snapshot()
        assert snap["ready"] is True and snap["stage"] == "就绪"

    def test_http_roundtrip_and_404(self, monkeypatch):
        s = self._fresh(monkeypatch)
        s.start()
        assert s._server is not None
        # 二次 start 是幂等空操作（已启动直接 return）
        server_obj = s._server
        s.start()
        assert s._server is server_obj

        port = server_obj.server_address[1]
        s.set("阶段X")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/progress", timeout=3) as r:
            assert r.status == 200
            data = json.loads(r.read().decode("utf-8"))
        assert data["stage"] == "阶段X" and data["ready"] is False
        assert isinstance(data["elapsed_sec"], float)
        # 带 query 的 /api/startup-progress 也命中
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/startup-progress?x=1", timeout=3
        ) as r:
            assert json.loads(r.read())["stage"] == "阶段X"
        # 未知路径 404
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=3)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404

        s.stop()
        assert s._server is None

    def test_start_bind_failure_silently_skipped(self, monkeypatch):
        from app import startup_progress as sp

        def _boom(*a, **kw):
            raise OSError("addr in use")

        monkeypatch.setattr(sp, "ThreadingHTTPServer", _boom)
        s = self._fresh(monkeypatch)
        s.start()  # 不抛：绑定失败仅告警
        assert s._server is None
        s.stop()  # 未启动时 stop 是空操作

    def test_stop_swallows_shutdown_error(self):
        from app.startup_progress import _State

        class BadServer:
            def shutdown(self):
                raise RuntimeError("shutdown boom")

        s = _State()
        s._server = BadServer()
        s.stop()  # 异常被吞掉，不向上抛
        assert s._server is None


# ==================== app/ops/diagnose ====================


class TestDiagnoseBasics:
    def test_result_shape(self):
        from app.ops import diagnose as dg

        r = dg._result("n", dg.PASS, "d", "a")
        assert r == {"name": "n", "status": "pass", "detail": "d", "advice": "a"}
        assert dg._result("n", dg.WARN, "d")["advice"] == ""

    def test_load_config_reads_file_and_ha_url_env(self, tmp_path, monkeypatch):
        from app.ops import diagnose as dg

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"ha": {"url": "http://cfg:8123"}, "x": 1}), encoding="utf-8")
        monkeypatch.setattr(dg, "CONFIG_PATH", cfg_file)
        monkeypatch.setenv("HA_URL", "http://env:8123")
        cfg = dg._load_config()
        assert cfg["ha"]["url"] == "http://env:8123"  # 环境变量覆盖
        assert cfg["x"] == 1

        monkeypatch.delenv("HA_URL")
        assert dg._load_config()["ha"]["url"] == "http://cfg:8123"

        cfg_file.write_text("{broken", encoding="utf-8")
        monkeypatch.setenv("HA_URL", "http://env2:8123")
        cfg = dg._load_config()
        assert cfg == {"ha": {"url": "http://env2:8123"}}  # 坏文件回退 {} + env 注入

        monkeypatch.setattr(dg, "CONFIG_PATH", tmp_path / "nope.json")
        assert dg._load_config() == {"ha": {"url": "http://env2:8123"}}

    def test_check_ports_pass_and_fail(self, monkeypatch):
        from app.ops import diagnose as dg

        sock, open_port = _listener()
        try:
            dead = _dead_port()
            monkeypatch.setattr(dg, "SERVICE_TARGETS", [
                ("服务A", "127.0.0.1", open_port),
                ("服务B", "127.0.0.1", dead),
            ])
            results = dg.check_ports()
        finally:
            sock.close()
        assert results[0]["status"] == "pass" and "已监听" in results[0]["detail"]
        assert str(open_port) in results[0]["name"]
        assert results[1]["status"] == "fail" and results[1]["advice"]


class TestDiagnoseHA:
    def test_no_url_warns(self):
        from app.ops import diagnose as dg

        r = dg.check_ha({})
        assert r[0]["status"] == "warn" and "未配置" in r[0]["detail"]

    def test_reachable_pass(self, monkeypatch):
        from app.ops import diagnose as dg

        class R:
            pass

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: R())
        r = dg.check_ha({"ha": {"url": "http://ha:8123/"}})
        assert r[0]["status"] == "pass"

    def test_http_error_means_alive(self, monkeypatch):
        from app.ops import diagnose as dg

        def _raise(req, timeout):
            raise urllib.error.HTTPError("http://ha:8123", 401, "unauth", None, None)

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        r = dg.check_ha({"ha": {"url": "http://ha:8123"}})
        assert r[0]["status"] == "pass" and "401" in r[0]["detail"]

    def test_unreachable_fails(self, monkeypatch):
        from app.ops import diagnose as dg

        def _raise(req, timeout):
            raise urllib.error.URLError("conn refused")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        r = dg.check_ha({"ha": {"url": "http://ha:8123"}})
        assert r[0]["status"] == "fail" and "不可达" in r[0]["detail"]


class TestDiagnoseCameras:
    @pytest.fixture
    def iso(self, tmp_path, monkeypatch):
        from app.ops import diagnose as dg

        monkeypatch.setattr(dg, "DB_PATH", tmp_path / "aether.db")
        return dg

    def test_no_cameras_warns(self, iso):
        r = iso.check_cameras({})
        assert r[0]["status"] == "warn" and "跳过" in r[0]["detail"]

    def test_legacy_single_camera_fallback(self, iso):
        r = iso.check_cameras({"vision": {"rtsp_url": "rtsp://127.0.0.1:1/x"}})
        assert any("旧版单摄配置" in res["name"] for res in r)

    def test_db_rows_preferred_over_legacy(self, iso, monkeypatch):
        conn = sqlite3.connect(iso.DB_PATH)
        conn.execute(
            "CREATE TABLE cameras (name TEXT, rtsp_url TEXT, enabled INT, source_type TEXT)")
        conn.execute("INSERT INTO cameras VALUES ('前院', 'rtsp://a/1', 1, 'rtsp')")
        conn.execute("INSERT INTO cameras VALUES (NULL, 'rtsp://a/2', 1, 'rtsp')")
        conn.execute("INSERT INTO cameras VALUES ('停用', 'rtsp://a/3', 0, 'rtsp')")
        conn.execute("INSERT INTO cameras VALUES ('子码流', '', 1, 'rtsp')")
        conn.commit()
        conn.close()
        rows = iso._load_cameras({"vision": {"rtsp_url": "rtsp://legacy/9"}})
        assert rows == [("前院", "rtsp://a/1"), ("(未命名)", "rtsp://a/2")]

    def test_corrupt_db_falls_back_to_legacy(self, iso):
        iso.DB_PATH.write_bytes(b"not a sqlite db at all")
        rows = iso._load_cameras({"vision": {"rtsp_url": "rtsp://legacy/9"}})
        assert rows == [("旧版单摄配置", "rtsp://legacy/9")]

    def test_tcp_unreachable_fails(self, iso):
        dead = _dead_port()
        r = iso.check_cameras({"vision": {"rtsp_url": f"rtsp://127.0.0.1:{dead}/s"}})
        assert r[0]["status"] == "fail" and "TCP" in r[0]["detail"]
        assert iso._tcp_reachable("127.0.0.1", dead) is False

    def _with_listener_and_cv2(self, monkeypatch, read_ok=None, raise_exc=None):
        sock, port = _listener()
        cap_result = {}

        class FakeCap:
            def __init__(self, *a, **kw):
                pass

            def read(self):
                if raise_exc:
                    raise raise_exc
                return read_ok, "frame"

            def release(self):
                cap_result["released"] = True

        fake_cv2 = types.SimpleNamespace(
            CAP_FFMPEG=0, VideoCapture=lambda url, backend: FakeCap())
        monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
        return sock, port, cap_result

    def test_stream_deep_check_passes(self, iso, monkeypatch):
        """取流成功：cv2 不再被重绑为 True（源码 bug 已修复），PASS 分支真实可达。"""
        sock, port, cap_result = self._with_listener_and_cv2(monkeypatch, read_ok=True)
        try:
            r = iso.check_cameras({"vision": {"rtsp_url": f"rtsp://127.0.0.1:{port}/s"}})
        finally:
            sock.close()
        assert r[0]["status"] == "pass"
        assert "取流成功" in r[0]["detail"]
        assert cap_result["released"] is True

    def test_stream_read_false_reports_credentials(self, iso, monkeypatch):
        """read() 返回 False → 网络通但取流失败（凭据/流格式）。"""
        sock, port, _ = self._with_listener_and_cv2(monkeypatch, read_ok=False)
        try:
            r = iso.check_cameras({"vision": {"rtsp_url": f"rtsp://127.0.0.1:{port}/s"}})
        finally:
            sock.close()
        assert r[0]["status"] == "fail"
        assert "取流失败" in r[0]["detail"] and "凭据" in r[0]["detail"]

    def test_stream_exception_fails(self, iso, monkeypatch):
        sock, port, _ = self._with_listener_and_cv2(
            monkeypatch, raise_exc=RuntimeError("device timeout"))
        try:
            r = iso.check_cameras({"vision": {"rtsp_url": f"rtsp://127.0.0.1:{port}/s"}})
        finally:
            sock.close()
        assert r[0]["status"] == "fail" and "取流异常" in r[0]["detail"]

    def test_reachable_without_opencv_warns(self, iso, monkeypatch):
        monkeypatch.setitem(sys.modules, "cv2", None)  # import cv2 → ImportError
        sock, port = _listener()
        try:
            r = iso.check_cameras({"vision": {"rtsp_url": f"rtsp://127.0.0.1:{port}/s"}})
        finally:
            sock.close()
        assert r[0]["status"] == "warn" and "opencv" in r[0]["detail"]


class TestDiagnoseDns:
    def test_no_hosts_warns(self):
        from app.ops import diagnose as dg

        assert dg.check_dns({})[0]["status"] == "warn"
        assert dg.check_dns({"llm_keys": [{"base_url": ""}]})[0]["status"] == "warn"

    def test_resolve_results(self, monkeypatch):
        from app.ops import diagnose as dg

        monkeypatch.setattr(
            dg, "_resolve",
            lambda host: (True, "1.2.3.4") if host == "a.com" else (False, "NXDOMAIN"),
        )
        cfg = {"llm_keys": [{"base_url": "https://a.com/v1"}, {"base_url": "https://b.com/v1"},
                            {"base_url": "not-a-url"}],
               "weather": {"host": "a.com"}}  # 与 llm 重复 → 去重
        results = dg.check_dns(cfg)
        by_name = {r["name"]: r for r in results}
        assert by_name["DNS：a.com"]["status"] == "pass"
        assert by_name["DNS：a.com"]["detail"] == "解析到 1.2.3.4"
        assert by_name["DNS：b.com"]["status"] == "fail"

    def test_resolve_error_shape(self):
        from app.ops import diagnose as dg

        ok, info = dg._resolve("nonexistent.invalid.invalid")
        assert ok is False and isinstance(info, str)


class TestDiagnoseResources:
    def test_disk_branches(self, monkeypatch):
        from app.ops import diagnose as dg

        big = types.SimpleNamespace(disk_usage=lambda p: types.SimpleNamespace(free=3 * 1024**3))
        monkeypatch.setattr(dg, "shutil", big)
        assert dg.check_resources()[0]["status"] == "pass"

        small = types.SimpleNamespace(disk_usage=lambda p: types.SimpleNamespace(free=0.5 * 1024**3))
        monkeypatch.setattr(dg, "shutil", small)
        r = dg.check_resources()
        assert r[0]["status"] == "fail" and "清理" in r[0]["advice"]

        def _boom(p):
            raise OSError("no perm")

        monkeypatch.setattr(dg, "shutil", types.SimpleNamespace(disk_usage=_boom))
        assert dg.check_resources()[0]["status"] == "warn"

    def test_memory_real_call_on_windows(self):
        from app.ops import diagnose as dg

        total, avail = dg._memory()
        if sys.platform == "win32":
            assert total is not None and total >= 0 and (avail is None or avail <= total)

    def test_memory_proc_parsing_and_low_mem_fail(self, monkeypatch):
        from app.ops import diagnose as dg

        class FakePath:
            _content = "MemTotal:       4000000 kB\nMemAvailable:   2000000 kB\n"

            def __init__(self, p):
                self.p = p

            def read_text(self, encoding=None):
                if str(self.p) == "/proc/meminfo":
                    return type(self)._content
                raise OSError(self.p)

        monkeypatch.setattr(dg, "Path", FakePath)
        total, avail = dg._memory()
        assert (total, avail) == (3906, 1953)

        FakePath._content = "MemTotal:       1000000 kB\n"
        results = dg.check_resources()
        mem = [r for r in results if r["name"] == "内存"][0]
        assert mem["status"] == "fail" and "2GB" in mem["advice"]

    def test_memory_none_warns(self, monkeypatch):
        from app.ops import diagnose as dg

        class _FakeCType:
            """支持 `ctypes.c_uint64 * 5`（结构体数组占位）。"""

            def __mul__(self, n):
                return self

            def __rmul__(self, n):
                return self

        fake_ctype = _FakeCType()
        fake_ctypes = types.SimpleNamespace(
            Structure=object,
            c_ulong=fake_ctype,
            c_uint64=fake_ctype,
            sizeof=lambda t: 64,
            byref=lambda x: x,
            windll=types.SimpleNamespace(
                kernel32=types.SimpleNamespace(GlobalMemoryStatusEx=lambda p: False)),
        )
        monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
        assert dg._memory() == (None, None)
        mem = [r for r in dg.check_resources() if r["name"] == "内存"][0]
        assert mem["status"] == "warn" and "无法读取" in mem["detail"]

    def test_arm_arch_note(self, monkeypatch):
        from app.ops import diagnose as dg

        monkeypatch.setattr(dg, "platform", types.SimpleNamespace(machine=lambda: "aarch64"))
        r = dg.check_resources()
        arch = [x for x in r if x["name"] == "CPU 架构"][0]
        assert "ARM" in arch["detail"]


class TestDiagnoseClock:
    def test_decode_console_bytes(self):
        from app.ops import diagnose as dg

        assert dg._decode_console_bytes("已同步".encode("utf-8")) == "已同步"
        assert dg._decode_console_bytes("已同步".encode("gbk")) == "已同步"  # utf-8 失败 → gbk
        assert "\ufffd" in dg._decode_console_bytes(b"\xff\xfe\x81\x3d")  # 全失败 → 替换

    def test_ntp_tool_synced_pass(self, monkeypatch):
        from app.ops import diagnose as dg

        out = types.SimpleNamespace(returncode=0, stdout="yes\n".encode("utf-8"))
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: out)
        r = dg.check_clock()
        assert r[0]["status"] == "pass" and r[0]["detail"] == "已同步"

    def test_ntp_tool_out_of_sync_warns(self, monkeypatch):
        from app.ops import diagnose as dg

        # 两个工具都 rc=0 但都没有“已同步”字样
        out = types.SimpleNamespace(returncode=0, stdout=b"no")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: out)
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("offline")))
        r = dg.check_clock()
        assert r[0]["status"] == "warn" and "未同步" in r[0]["detail"]

    def test_tools_fail_http_date_synced(self, monkeypatch):
        from datetime import datetime, timezone

        from app.ops import diagnose as dg

        def _tool_fail(*a, **kw):
            raise FileNotFoundError("no timedatectl/w32tm")

        monkeypatch.setattr(subprocess, "run", _tool_fail)

        class R:
            headers = {"Date": format_datetime(datetime.now(timezone.utc), usegmt=True)}

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: R())
        r = dg.check_clock()
        assert r[0]["status"] == "pass" and "HTTP 对时" in r[0]["detail"]

    def test_http_date_skew_too_large_fails(self, monkeypatch):
        from app.ops import diagnose as dg
        from datetime import datetime, timezone, timedelta

        def _tool_fail(*a, **kw):
            raise FileNotFoundError("no tools")

        monkeypatch.setattr(subprocess, "run", _tool_fail)
        stale = datetime.now(timezone.utc) - timedelta(minutes=30)

        class R:
            headers = {"Date": format_datetime(stale, usegmt=True)}

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: R())
        r = dg.check_clock()
        assert r[0]["status"] == "fail" and "偏差" in r[0]["detail"]

    def test_date_header_missing_then_offline_warns(self, monkeypatch):
        from app.ops import diagnose as dg

        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: types.SimpleNamespace(returncode=1, stdout=b""))
        calls = {"n": 0}

        class R:
            headers = {}  # 无 Date 头

        def _urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return R()
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        r = dg.check_clock()
        assert calls["n"] == 2  # 两个候选站点都试过
        assert r[0]["status"] == "warn" and "无法校时" in r[0]["detail"]

    def test_all_channels_offline_warns(self, monkeypatch):
        from app.ops import diagnose as dg

        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("x")))
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("x")))
        r = dg.check_clock()
        assert r[0]["status"] == "warn" and "手动核对" in r[0]["advice"]


class TestRunAll:
    def test_report_structure_and_summary(self, tmp_path, monkeypatch):
        from app.ops import diagnose as dg

        monkeypatch.setattr(dg, "CONFIG_PATH", tmp_path / "config.json")
        monkeypatch.setattr(dg, "check_ports", lambda: [dg._result("p", dg.PASS, "ok")])
        monkeypatch.setattr(dg, "check_ha", lambda cfg: [dg._result("ha", dg.WARN, "w")])
        monkeypatch.setattr(dg, "check_cameras", lambda cfg: [])
        monkeypatch.setattr(dg, "check_dns", lambda cfg: [dg._result("dns", dg.FAIL, "f")])
        monkeypatch.setattr(dg, "check_resources", lambda: [])
        monkeypatch.setattr(dg, "check_clock",
                            lambda: (_ for _ in ()).throw(RuntimeError("kaboom")))
        report = dg.run_all(timeout=10)
        assert report["environment"] == "host"
        assert report["platform"]
        assert report["created_at"].endswith("+00:00") or "T" in report["created_at"]
        names = [c["name"] for c in report["checks"]]
        assert names == ["p", "ha", "dns", "检查组异常"]
        assert report["summary"] == {"pass": 1, "warn": 1, "fail": 2}
        assert report["checks"][3]["status"] == "fail" and "kaboom" in report["checks"][3]["detail"]


# ==================== app/services/config_probes ====================


class TestProbeResult:
    def test_to_dict_variants(self):
        from app.services.config_probes import ProbeResult

        assert ProbeResult(ok=True).to_dict() == {"ok": True}
        r = ProbeResult(ok=False, reason="unreachable", detail="x", extra={"attempted": 1})
        assert r.to_dict() == {"ok": False, "reason": "unreachable", "detail": "x",
                               "attempted": 1}


class TestClassifyHttpxError:
    def test_status_errors(self):
        from app.services.config_probes import _classify_httpx_error

        req = httpx.Request("GET", "https://x")
        e401 = httpx.HTTPStatusError("m", request=req,
                                     response=httpx.Response(401, request=req))
        assert _classify_httpx_error(e401)[0] == "unauthorized"
        e403 = httpx.HTTPStatusError("m", request=req,
                                     response=httpx.Response(403, request=req))
        assert _classify_httpx_error(e403)[0] == "unauthorized"
        e500 = httpx.HTTPStatusError("m", request=req,
                                     response=httpx.Response(500, request=req))
        reason, detail = _classify_httpx_error(e500)
        assert reason == "error" and "500" in detail

    def test_transport_and_other_errors(self):
        from app.services.config_probes import _classify_httpx_error

        for exc in (httpx.ConnectError("refused"), httpx.TimeoutException("t"),
                    httpx.UnsupportedProtocol("u")):
            assert _classify_httpx_error(exc)[0] == "unreachable"
        reason, detail = _classify_httpx_error(ValueError("boom"))
        assert reason == "error" and detail == "boom"


class _FakeAsyncClient:
    """post/get 依次吐出 preset 响应的 httpx.AsyncClient 替身。"""

    last_request: dict = {}
    responses: list = []
    raise_on_call: Exception | None = None

    def __init__(self, timeout=None, transport=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_request = {"method": "POST", "url": url, "json": json,
                                   "headers": headers}
        return self._next()

    async def get(self, url, headers=None, params=None):
        type(self).last_request = {"method": "GET", "url": url, "headers": headers,
                                   "params": params}
        return self._next()

    def _next(self):
        if type(self).raise_on_call is not None:
            raise type(self).raise_on_call
        return type(self).responses.pop(0)


@pytest.fixture
def fake_http(monkeypatch):
    from app.services import config_probes as cp

    _FakeAsyncClient.last_request = {}
    _FakeAsyncClient.responses = []
    _FakeAsyncClient.raise_on_call = None
    monkeypatch.setattr(cp.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


class TestProbeExa:
    def test_empty_key_is_anonymous(self):
        from app.services.config_probes import probe_exa

        for key in ("", "   "):
            r = asyncio.run(probe_exa(key))
            assert r.ok is True and r.extra.get("anonymous") is True

    def test_http_401_unauthorized(self, fake_http):
        from app.services.config_probes import probe_exa

        fake_http.responses = [_FakeResp(401)]
        r = asyncio.run(probe_exa("bad-key"))
        assert (r.ok, r.reason) == (False, "unauthorized")
        assert "401" in r.detail
        assert "exaApiKey=bad-key" in fake_http.last_request["url"]

    def test_http_500_error(self, fake_http):
        from app.services.config_probes import probe_exa

        fake_http.responses = [_FakeResp(500, text="oops")]
        r = asyncio.run(probe_exa("k"))
        assert (r.ok, r.reason) == (False, "error")
        assert "500" in r.detail and "oops" in r.detail

    def test_sse_jsonrpc_error_is_unauthorized(self, fake_http):
        from app.services.config_probes import probe_exa

        body = 'data: {"jsonrpc":"2.0","error":{"message":"Invalid API key"}}\n'
        fake_http.responses = [_FakeResp(200, text=body)]
        r = asyncio.run(probe_exa("k"))
        assert (r.ok, r.reason) == (False, "unauthorized")
        assert "Invalid API key" in r.detail

    def test_sse_is_error_with_api_key_text(self, fake_http):
        from app.services.config_probes import probe_exa

        body = ('data: {"jsonrpc":"2.0","result":{"isError":true,"content":'
                '[{"type":"text","text":"error (401): Invalid API key"}]}}\n')
        fake_http.responses = [_FakeResp(200, text=body)]
        r = asyncio.run(probe_exa("k"))
        assert r.reason == "unauthorized" and "401" in r.detail

    def test_sse_is_error_other_reason(self, fake_http):
        from app.services.config_probes import probe_exa

        body = 'data: {"jsonrpc":"2.0","result":{"isError":true,"content":[{"text":"quota exhausted"}]}}\n'
        fake_http.responses = [_FakeResp(200, text=body)]
        r = asyncio.run(probe_exa("k"))
        assert (r.ok, r.reason) == (False, "error") and "quota" in r.detail

    def test_clean_result_ok(self, fake_http):
        from app.services.config_probes import probe_exa

        body = 'data: {"jsonrpc":"2.0","result":{"content":[]}}\n'
        fake_http.responses = [_FakeResp(200, text=body)]
        r = asyncio.run(probe_exa("k"))
        assert r.ok is True and r.detail == "Exa key 验证通过"

    def test_plain_json_body_ok(self, fake_http):
        from app.services.config_probes import probe_exa

        fake_http.responses = [_FakeResp(200, text='{"result": {"isError": false}}')]
        r = asyncio.run(probe_exa("k"))
        assert r.ok is True

    def test_garbage_body_falls_through_ok(self, fake_http):
        from app.services.config_probes import probe_exa

        # 一行以 { 开头但解析失败（走 JSONDecodeError continue），其余行被忽略
        fake_http.responses = [_FakeResp(200, text='{"truncated\nnoise-line\n')]
        r = asyncio.run(probe_exa("k"))
        assert r.ok is True

    def test_connect_error_unreachable(self, fake_http):
        from app.services.config_probes import probe_exa

        fake_http.raise_on_call = httpx.ConnectError("refused")
        r = asyncio.run(probe_exa("k"))
        assert (r.ok, r.reason) == (False, "unreachable")

    def test_unexpected_exception_maps_to_error(self, fake_http):
        from app.services.config_probes import probe_exa

        fake_http.raise_on_call = ValueError("weird")
        r = asyncio.run(probe_exa("k"))
        assert (r.ok, r.reason) == (False, "error") and "weird" in r.detail


class TestBuildQweatherJwt:
    def _pem(self) -> str:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        return key.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()

    def test_raw_base64_key_branch(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from app.services.config_probes import _build_qweather_jwt

        pem = self._pem()
        # 与和风配置存储一致：raw = PEM 去掉 armor 头尾后的 base64 文本（DER 的 b64）
        raw = "".join(pem.strip().splitlines()[1:-1])
        token = _build_qweather_jwt("host", "kid1", "sub1", raw)
        h, p, sig = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        header = json.loads(base64.urlsafe_b64decode(pad(h)))
        payload = json.loads(base64.urlsafe_b64decode(pad(p)))
        assert header == {"alg": "EdDSA", "kid": "kid1"}
        assert payload["sub"] == "sub1" and payload["exp"] > payload["iat"]
        # 签名可用同一把公钥验证
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        key.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{h}.{p}".encode())

    def test_pem_passthrough_branch(self):
        from app.services.config_probes import _build_qweather_jwt

        token = _build_qweather_jwt("host", "kid", "sub", self._pem())
        assert token.count(".") == 2

    def test_missing_fields_raise_valueerror(self):
        from app.services.config_probes import _build_qweather_jwt

        pem = self._pem()
        with pytest.raises(ValueError, match="private_key"):
            _build_qweather_jwt("h", "k", "s", "")
        with pytest.raises(ValueError, match="kid"):
            _build_qweather_jwt("h", "", "s", pem)
        with pytest.raises(ValueError, match="sub"):
            _build_qweather_jwt("h", "k", "", pem)

    def test_invalid_key_raises(self):
        from app.services.config_probes import _build_qweather_jwt

        junk = base64.b64encode(b"definitely not a pem key").decode()
        with pytest.raises(Exception):
            _build_qweather_jwt("h", "k", "s", junk)


class TestProbeWeather:
    def _pem_b64(self) -> str:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        # 和风配置的 raw 格式 = PEM 去掉 armor 头尾后的 base64 文本（DER 的 b64）
        return "".join(pem.strip().splitlines()[1:-1])

    def test_empty_host_bad_format(self):
        from app.services.config_probes import probe_weather

        r = asyncio.run(probe_weather("  ", "k", "s", "x"))
        assert (r.ok, r.reason) == (False, "bad_format")

    def test_missing_kid_bad_format(self):
        from app.services.config_probes import probe_weather

        r = asyncio.run(probe_weather("h", "", "s", self._pem_b64()))
        assert r.reason == "bad_format" and "kid" in r.detail

    def test_invalid_key_raises_generic(self, monkeypatch):
        """load_pem_private_key 抛非 ValueError 异常 →「private_key 无效」分支。"""
        from app.services import config_probes as cp
        from app.services.config_probes import probe_weather

        def _boom(*a, **kw):
            raise TypeError("unsupported key type")

        monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.serialization",
                            types.SimpleNamespace(load_pem_private_key=_boom))
        r = asyncio.run(probe_weather("h", "k", "s", "whatever-not-pem"))
        assert r.reason == "bad_format" and "private_key" in r.detail
        assert cp  # 引用避免 lint

    def test_success_and_request_shape(self, fake_http):
        from app.services.config_probes import probe_weather

        fake_http.responses = [_FakeResp(200, payload={"code": "200"})]
        r = asyncio.run(probe_weather("devapi.qweather.com", "k", "s", self._pem_b64()))
        assert r.ok is True and "验证通过" in r.detail
        req = fake_http.last_request
        assert req["url"] == "https://devapi.qweather.com/geo/v2/city/lookup"
        assert req["headers"]["Authorization"].startswith("Bearer ")
        assert req["params"] == {"location": "auto"}

    def test_401_unauthorized(self, fake_http):
        from app.services.config_probes import probe_weather

        fake_http.responses = [_FakeResp(401)]
        r = asyncio.run(probe_weather("h", "k", "s", self._pem_b64()))
        assert r.reason == "unauthorized"

    def test_500_error(self, fake_http):
        from app.services.config_probes import probe_weather

        fake_http.responses = [_FakeResp(503, text="down")]
        r = asyncio.run(probe_weather("h", "k", "s", self._pem_b64()))
        assert r.reason == "error" and "503" in r.detail

    def test_connect_error_unreachable(self, fake_http):
        from app.services.config_probes import probe_weather

        fake_http.raise_on_call = httpx.ConnectTimeout("slow")
        r = asyncio.run(probe_weather("h", "k", "s", self._pem_b64()))
        assert r.reason == "unreachable"


# ==================== app/routes/ops_routes ====================


@pytest.fixture
def audit_tmp(tmp_path, monkeypatch):
    from app.ops import audit

    audit_dir = tmp_path / "audit_out"
    monkeypatch.setattr(audit, "AUDIT_DIR", audit_dir)
    monkeypatch.setattr(audit, "AUDIT_FILE", audit_dir / "ops_audit.jsonl")
    return audit_dir


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.auth import get_current_admin
    from app.routes import ops_routes

    test_app = FastAPI()
    test_app.include_router(ops_routes.router, prefix="/api")
    test_app.dependency_overrides[get_current_admin] = lambda: {
        "username": "tester", "user_id": "u1"}
    return TestClient(test_app)


def _audit_lines(audit_dir: Path) -> list[dict]:
    f = audit_dir / "ops_audit.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]


class TestOpsRoutesBasic:
    def test_export_diagnostics(self, client, monkeypatch):
        from app.routes import ops_routes

        captured = {}

        async def fake_build(operator):
            captured["operator"] = operator
            return b"zip-bytes", "aether-diag-x.zip"

        monkeypatch.setattr(ops_routes, "build_diagnostic_package", fake_build)
        resp = client.get("/api/ops/diagnostics")
        assert resp.status_code == 200
        assert resp.content == b"zip-bytes"
        assert resp.headers["content-type"] == "application/zip"
        assert "aether-diag-x.zip" in resp.headers["content-disposition"]
        assert captured["operator"] == "tester"

    def test_recent_audit(self, client, monkeypatch):
        from app.ops import audit
        from app.routes import ops_routes

        monkeypatch.setattr(ops_routes.audit, "tail", lambda limit=50: [{"action": "x"}])
        assert audit  # 引用避免 lint
        resp = client.get("/api/ops/audit")
        assert resp.status_code == 200
        assert resp.json()["data"] == [{"action": "x"}]

    def test_clear_audit_records_itself(self, client, audit_tmp, monkeypatch):
        from app.ops import audit

        (audit_tmp).mkdir(parents=True)
        (audit_tmp / "ops_audit.jsonl").write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
        resp = client.delete("/api/ops/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == {"cleared": True, "removed": 2}
        last = _audit_lines(audit_tmp)[-1]
        assert last["action"] == "audit_clear" and last["operator"] == "tester"
        assert last["detail"] == {"removed_entries": 2}
        # 清空后文件里只剩清空动作自身这条记录
        assert [e["action"] for e in audit.tail()] == ["audit_clear"]

    def test_run_diagnose(self, client, audit_tmp, monkeypatch):
        from app.ops import diagnose
        from app.routes import ops_routes

        fake = {"environment": "host", "summary": {"pass": 1, "warn": 0, "fail": 0}}
        monkeypatch.setattr(ops_routes.diagnose, "run_all", lambda timeout=40.0: fake)
        assert diagnose  # 模块已随路由加载
        resp = client.post("/api/ops/diagnose")
        assert resp.status_code == 200
        assert resp.json()["data"] == fake
        assert _audit_lines(audit_tmp)[-1]["action"] == "diagnose_run"

    def test_version_info(self, client, tmp_path, monkeypatch):
        from app.core.version import get_version
        from app.ops import upgrade
        from app.routes import ops_routes

        sock = tmp_path / "docker.sock"
        sock.write_bytes(b"")
        monkeypatch.setattr(ops_routes.upgrade, "DOCKER_SOCK", sock)
        monkeypatch.setattr(ops_routes.upgrade, "upgrade_history",
                            lambda limit=10: [{"to_version": "1.1.0"}])
        assert upgrade and get_version
        resp = client.get("/api/ops/version")
        body = resp.json()["data"]
        assert body["version"] == get_version()
        assert body["docker_socket"] is True  # 布尔而非 str(bool)
        assert body["history"] == [{"to_version": "1.1.0"}]


class TestOpsRoutesPack:
    def test_start_export(self, client, audit_tmp, monkeypatch):
        from app.routes import ops_routes

        async def fake_start(operator, notes):
            assert operator == "tester" and notes == "n"  # 路由层已 strip
            return {"started": True}

        monkeypatch.setattr(ops_routes.pack_export, "start_export", fake_start)
        resp = client.post("/api/ops/update-pack/export", json={"notes": "  n  "})
        assert resp.status_code == 200 and resp.json()["data"] == {"started": True}
        assert _audit_lines(audit_tmp)[-1]["action"] == "pack_export_start"

    def test_export_status(self, client, monkeypatch):
        from app.routes import ops_routes

        monkeypatch.setattr(ops_routes.pack_export, "export_status",
                            lambda: {"status": "running"})
        resp = client.get("/api/ops/update-pack/export/status")
        assert resp.json()["data"] == {"status": "running"}

    def test_download_no_file_404(self, client, monkeypatch):
        from app.routes import ops_routes

        monkeypatch.setattr(ops_routes.pack_export, "export_status",
                            lambda: {"status": "idle", "file": ""})
        assert client.get("/api/ops/update-pack/download").status_code == 404

    def test_download_file_missing_404(self, client, tmp_path, monkeypatch):
        from app.routes import ops_routes

        monkeypatch.setattr(ops_routes.pack_export, "PACK_DIR", tmp_path)
        monkeypatch.setattr(ops_routes.pack_export, "export_status",
                            lambda: {"status": "done", "file": "ghost.tar.gz"})
        assert client.get("/api/ops/update-pack/download").status_code == 404

    def test_download_ok(self, client, tmp_path, monkeypatch):
        from app.routes import ops_routes

        pack = tmp_path / "aether-update-1.0.0.tar.gz"
        pack.write_bytes(b"tar-content")
        monkeypatch.setattr(ops_routes.pack_export, "PACK_DIR", tmp_path)
        monkeypatch.setattr(ops_routes.pack_export, "export_status",
                            lambda: {"status": "done", "file": pack.name})
        resp = client.get("/api/ops/update-pack/download")
        assert resp.status_code == 200 and resp.content == b"tar-content"

    def test_list_local(self, client, monkeypatch):
        from app.routes import ops_routes

        monkeypatch.setattr(ops_routes.pack_export, "scan_local_packs",
                            lambda: [{"name": "aether-update-1.0.0.tar.gz"}])
        resp = client.get("/api/ops/update-pack/local")
        assert resp.json()["data"] == [{"name": "aether-update-1.0.0.tar.gz"}]

    def test_apply_local_ok(self, client, audit_tmp, monkeypatch):
        from app.routes import ops_routes

        async def fake_apply(name, operator):
            return {"to_version": "1.2.0"}

        monkeypatch.setattr(ops_routes.pack_export, "apply_local_pack", fake_apply)
        resp = client.post("/api/ops/update-pack/local/aether-update-1.2.0.tar.gz/apply")
        assert resp.status_code == 200 and resp.json()["data"] == {"to_version": "1.2.0"}

    def test_apply_local_appexception_maps_status(self, client, monkeypatch):
        from app.core.exceptions import AppException
        from app.routes import ops_routes

        async def fake_apply(name, operator):
            raise AppException("升级包不存在", http_status=404)

        monkeypatch.setattr(ops_routes.pack_export, "apply_local_pack", fake_apply)
        resp = client.post("/api/ops/update-pack/local/aether-update-1.2.0.tar.gz/apply")
        assert resp.status_code == 404 and "升级包不存在" in resp.json()["detail"]

    def test_apply_local_valueerror_maps_400(self, client, monkeypatch):
        from app.routes import ops_routes

        async def fake_apply(name, operator):
            raise ValueError("校验失败")

        monkeypatch.setattr(ops_routes.pack_export, "apply_local_pack", fake_apply)
        resp = client.post("/api/ops/update-pack/local/aether-update-1.2.0.tar.gz/apply")
        assert resp.status_code == 400 and "校验失败" in resp.json()["detail"]


class TestOpsRoutesBackup:
    def test_create_backup_ok(self, client, monkeypatch):
        from app.routes import ops_routes

        async def fake_exec(fn, *a):
            return {"fn": fn.__name__, "operator": a[0]}

        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec)
        resp = client.post("/api/ops/backups")
        assert resp.status_code == 200
        assert resp.json()["data"] == {"fn": "create_backup", "operator": "tester"}

    def test_create_backup_runtime_error_400(self, client, monkeypatch):
        from app.routes import ops_routes

        async def fake_exec(fn, *a):
            raise RuntimeError("磁盘不足")

        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec)
        resp = client.post("/api/ops/backups")
        assert resp.status_code == 400 and "磁盘不足" in resp.json()["detail"]

    def test_list_backups(self, client, monkeypatch):
        from app.routes import ops_routes

        async def fake_exec(fn, *a):
            return [{"name": "aether-backup-x.tar.gz"}]

        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec)
        assert client.get("/api/ops/backups").json()["data"][0]["name"].startswith("aether")

    def test_delete_backup(self, client, monkeypatch):
        from app.routes import ops_routes

        async def fake_exec(fn, *a):
            return True

        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec)
        resp = client.delete("/api/ops/backups/aether-backup-x.tar.gz")
        assert resp.json()["data"] == {"deleted": True, "name": "aether-backup-x.tar.gz"}

        async def fake_exec_false(fn, *a):
            return False

        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec_false)
        assert client.delete("/api/ops/backups/aether-backup-y.tar.gz").status_code == 404

        async def fake_exec_valueerror(fn, *a):
            raise ValueError("非法名")

        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec_valueerror)
        # 不能用带 ../ 的名字（httpx 会做 URL 归一化弹出路径段）
        assert client.delete("/api/ops/backups/bad-name.tar.gz").status_code == 400

    def test_validate_backup(self, client, monkeypatch):
        from app.routes import ops_routes

        async def fake_exec(fn, *a):
            return {"has_config": True}

        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec)
        resp = client.get("/api/ops/backups/b/validate")
        assert resp.json()["data"] == {"has_config": True}

        async def raise_valueerror(fn, *a):
            raise ValueError("包损坏")

        monkeypatch.setattr(ops_routes, "_run_in_executor", raise_valueerror)
        assert client.get("/api/ops/backups/b/validate").status_code == 400

        async def raise_filenotfound(fn, *a):
            raise FileNotFoundError("不存在")

        monkeypatch.setattr(ops_routes, "_run_in_executor", raise_filenotfound)
        assert client.get("/api/ops/backups/b/validate").status_code == 404

    def test_restore_requires_confirm(self, client):
        resp = client.post("/api/ops/backups/b/restore", json={"confirm": False})
        assert resp.status_code == 400 and "confirm" in resp.json()["detail"]

    def test_restore_validate_errors(self, client, monkeypatch):
        from app.routes import ops_routes

        async def raise_valueerror(fn, *a):
            raise ValueError("包损坏")

        monkeypatch.setattr(ops_routes, "_run_in_executor", raise_valueerror)
        assert client.post("/api/ops/backups/b/restore",
                           json={"confirm": True}).status_code == 400

        async def raise_filenotfound(fn, *a):
            raise FileNotFoundError("不存在")

        monkeypatch.setattr(ops_routes, "_run_in_executor", raise_filenotfound)
        assert client.post("/api/ops/backups/b/restore",
                           json={"confirm": True}).status_code == 404

    def test_restore_ok_closes_db_and_returns(self, client, monkeypatch):
        from app.core.database import Database
        from app.routes import ops_routes

        calls = []

        async def fake_exec(fn, *a):
            calls.append((fn.__name__, a))
            if fn.__name__ == "validate_backup":
                return {"ok": True}
            return {"restored": True}

        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec)
        monkeypatch.setattr(Database, "_instance", None, raising=False)
        resp = client.post("/api/ops/backups/b/restore", json={"confirm": True})
        assert resp.status_code == 200 and resp.json()["data"] == {"restored": True}
        assert [c[0] for c in calls] == ["validate_backup", "restore_backup"]

    def test_restore_closes_open_database(self, client, monkeypatch):
        """恢复前会关掉打开的数据库连接（覆盖 Database._instance 非空分支）。"""
        from app.core.database import Database
        from app.routes import ops_routes

        closed = {"n": 0}

        async def fake_close():
            closed["n"] += 1

        async def fake_exec(fn, *a):
            return {"ok": True} if fn.__name__ == "validate_backup" else {"restored": True}

        sentinel = object()
        monkeypatch.setattr(Database, "_instance", sentinel, raising=False)
        monkeypatch.setattr(Database, "close", fake_close, raising=False)
        monkeypatch.setattr(ops_routes, "_run_in_executor", fake_exec)
        resp = client.post("/api/ops/backups/b/restore", json={"confirm": True})
        assert resp.status_code == 200
        assert closed["n"] == 1  # 连接已关闭


# ==================== app/ops/pack_export ====================


@pytest.fixture
def pe_iso(tmp_path, monkeypatch):
    """pack_export 隔离：PACK_DIR / STAGING_DIR / BASE_DIR 全指 tmp。"""
    from app.ops import pack_export as pe

    pack_dir = tmp_path / "backups"
    monkeypatch.setattr(pe, "PACK_DIR", pack_dir)
    monkeypatch.setattr(pe, "STAGING_DIR", pack_dir / ".export-staging")
    monkeypatch.setattr(pe, "BASE_DIR", tmp_path)
    monkeypatch.setattr(pe, "_state", {"status": "idle", "staged_bytes": 0,
                                       "total_bytes": 0, "file": "", "error": ""})
    return pe, tmp_path, pack_dir


class TestPackMetaReaders:
    def test_read_min_compatible(self, pe_iso):
        pe, tmp_path, _ = pe_iso
        (tmp_path / "version.json").write_text(
            json.dumps({"version": "1.0.0", "min_compatible": "0.9.0"}), encoding="utf-8")
        assert pe._read_min_compatible() == "0.9.0"

        (tmp_path / "version.json").write_text("{broken", encoding="utf-8")
        monkey_ver = "9.8.7"
        import unittest.mock as mock

        with mock.patch.object(pe, "get_version", return_value=monkey_ver):
            assert pe._read_min_compatible() == monkey_ver  # 坏文件回退当前版本
            (tmp_path / "version.json").write_text("{}", encoding="utf-8")
            assert pe._read_min_compatible() == monkey_ver  # 缺字段也回退

    def test_read_version_notes(self, pe_iso):
        pe, tmp_path, _ = pe_iso
        (tmp_path / "version.json").write_text(
            json.dumps({"notes": "修复若干问题"}), encoding="utf-8")
        assert pe._read_version_notes() == "修复若干问题"
        (tmp_path / "version.json").write_text("{broken", encoding="utf-8")
        assert pe._read_version_notes() == ""
        (tmp_path / "version.json").write_text("{}", encoding="utf-8")
        assert pe._read_version_notes() == ""


class TestExportStatus:
    def test_done_with_existing_file(self, pe_iso):
        pe, tmp_path, pack_dir = pe_iso
        pack_dir.mkdir(parents=True)
        (pack_dir / "aether-update-1.0.0.tar.gz").write_bytes(b"x" * 123)
        pe._state.update(status="done", file="aether-update-1.0.0.tar.gz")
        s = pe.export_status()
        assert s["size_bytes"] == 123 and s["status"] == "done"

    def test_done_with_missing_file(self, pe_iso):
        pe, _, pack_dir = pe_iso
        pack_dir.mkdir(parents=True)
        pe._state.update(status="done", file="gone.tar.gz")
        assert pe.export_status()["size_bytes"] == 0


class TestStartExport:
    def test_busy_409(self, pe_iso):
        from app.core.exceptions import AppException

        pe, _, _ = pe_iso
        pe._state.update(status="running")
        with pytest.raises(AppException) as ei:
            asyncio.run(pe.start_export("t"))
        assert ei.value.http_status == 409

    def test_docker_sock_missing_400(self, pe_iso, monkeypatch):
        from app.core.exceptions import AppException
        from app.ops import upgrade as up

        pe, _, _ = pe_iso
        monkeypatch.setattr(up, "DOCKER_SOCK", pe.PACK_DIR / "nope.sock")
        with pytest.raises(AppException) as ei:
            asyncio.run(pe.start_export("t"))
        assert ei.value.http_status == 400 and "docker.sock" in ei.value.message

    def test_start_ok_marks_running(self, pe_iso, monkeypatch):
        from app.ops import upgrade as up

        pe, _, _ = pe_iso
        sock = pe.PACK_DIR / "docker.sock"
        pe.PACK_DIR.mkdir(parents=True)
        sock.write_bytes(b"")
        monkeypatch.setattr(up, "DOCKER_SOCK", sock)

        called = {}

        async def fake_job(operator, notes):
            called["args"] = (operator, notes)

        monkeypatch.setattr(pe, "_export_job", fake_job)
        result = asyncio.run(pe.start_export("tester", "notes-1"))
        assert result == {"started": True}
        assert pe._state["status"] == "running"
        assert called["args"] == ("tester", "notes-1")
        # 后台任务在当前 loop run 完成后再断言（fake 立即返回）
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            asyncio.sleep(0.01))


def _write_image_tar(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


class TestExportJob:
    def _setup(self, pe_iso, monkeypatch, tag_status=201):
        pe, tmp_path, pack_dir = pe_iso
        (tmp_path / "version.json").write_text(
            json.dumps({"version": "1.0.0", "min_compatible": "0.9.0",
                        "notes": "rel notes"}), encoding="utf-8")
        calls = []

        async def fake_docker(method, path, timeout=300.0, **kw):
            calls.append((method, path))
            if path.endswith("/tag"):
                return _FakeResp(tag_status, text="tag err")
            return _FakeResp(200, {"Size": 4321})

        monkeypatch.setattr(pe.upgrade, "_docker", fake_docker)

        payload = b"fake-docker-image-layer" * 50

        class StreamResp:
            status_code = 200

            async def aiter_bytes(self, n):
                yield payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Client:
            def __init__(self, transport=None, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url):
                assert method == "GET" and "aether-app" in url
                return StreamResp()

        monkeypatch.setattr(pe.httpx, "AsyncClient", Client)
        return pe, payload, calls

    def test_export_success(self, pe_iso, monkeypatch, audit_tmp):
        pe, payload, calls = self._setup(pe_iso, monkeypatch)
        asyncio.run(pe._export_job("tester", "手工备注"))
        assert pe._state["status"] == "done" and pe._state["error"] == ""
        assert pe._state["total_bytes"] == 4321
        assert ("POST", "/images/aether-app:latest/tag") in calls
        out = pe.PACK_DIR / f"aether-update-{pe.get_version()}.tar.gz"
        assert out.exists() and out.stat().st_size > 0
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
            manifest = json.loads(tf.extractfile("manifest.json").read().decode("utf-8"))
            img = tf.extractfile("images/aether.tar").read()
        assert "manifest.json" in names and "images/aether.tar" in names
        assert img == payload
        assert manifest["notes"] == "手工备注"  # 显式 notes 优先于 version.json
        assert manifest["min_compatible"] == "0.9.0"
        assert manifest["images"][0]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert manifest["images"][0]["size_bytes"] == len(payload)
        audits = _audit_lines(audit_tmp)
        assert audits[-1]["action"] == "pack_export" and audits[-1]["detail"]["file"] == out.name
        # staging 中间目录已收尾（保留但状态 done）；状态文件可被再次读取
        assert pe.export_status()["file"] == out.name

    def test_export_docker_tag_failure_sets_error(self, pe_iso, monkeypatch, audit_tmp):
        pe, payload, _ = self._setup(pe_iso, monkeypatch, tag_status=500)
        asyncio.run(pe._export_job("tester", ""))
        assert pe._state["status"] == "error"
        assert "docker tag 失败" in pe._state["error"]

    def test_export_save_failure_sets_error(self, pe_iso, monkeypatch):
        pe, _, _ = self._setup(pe_iso, monkeypatch)

        class BadStreamResp:
            status_code = 404

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Client:
            def __init__(self, transport=None, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url):
                return BadStreamResp()

        monkeypatch.setattr(pe.httpx, "AsyncClient", Client)
        asyncio.run(pe._export_job("tester", ""))
        assert pe._state["status"] == "error" and "docker save 失败" in pe._state["error"]


class TestScanLocalPacksMine:
    def test_scan_sorted_and_meta(self, pe_iso, monkeypatch):
        """与既有 test_pack_export 互补：本文件运行时自证 scan 行为。"""
        import os
        from datetime import datetime

        pe, _, pack_dir = pe_iso
        pack_dir.mkdir(parents=True)
        for i, name in enumerate(["aether-update-1.0.0.tar.gz",
                                  "aether-update-1.1.0.tar.gz", "unrelated.txt"]):
            f = pack_dir / name
            f.write_bytes(b"x" * (i + 1))
            os.utime(f, (1700000000 + i,) * 2)
        packs = pe.scan_local_packs()
        assert [p["name"] for p in packs] == ["aether-update-1.1.0.tar.gz",
                                              "aether-update-1.0.0.tar.gz"]
        assert packs[0]["size_bytes"] == 2
        assert packs[0]["created_at"] == datetime.fromtimestamp(
            1700000001).strftime("%Y-%m-%d %H:%M")  # 本地时区 mtime 格式化
        assert packs[0]["version"] == ""  # 假包无 manifest → 版本未知

    def test_scan_missing_dir_returns_empty(self, pe_iso, monkeypatch):
        pe, tmp_path, _ = pe_iso
        monkeypatch.setattr(pe, "PACK_DIR", tmp_path / "nope")
        assert pe.scan_local_packs() == []

    def test_local_pack_path_bad_name_400(self, pe_iso):
        from app.core.exceptions import AppException

        pe, _, _ = pe_iso
        with pytest.raises(AppException) as ei:
            pe.local_pack_path("aether-update-../../evil.tar.gz")
        assert ei.value.http_status == 400 and ei.value.code == "pack_bad_name"


class TestPeekPackMeta:
    def test_dir_entry_manifest_returns_none(self, tmp_path):
        from app.ops import pack_export as pe

        p = tmp_path / "aether-update-1.0.0.tar.gz"
        with tarfile.open(p, "w:gz") as tf:
            info = tarfile.TarInfo("manifest.json")
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        assert pe.peek_pack_meta(p) is None

    def test_corrupt_or_missing_manifest_returns_none(self, tmp_path):
        from app.ops import pack_export as pe

        corrupt = tmp_path / "aether-update-1.0.0.tar.gz"
        corrupt.write_bytes(b"this is not a gzip")
        assert pe.peek_pack_meta(corrupt) is None

        nomani = tmp_path / "aether-update-1.0.1.tar.gz"
        with tarfile.open(nomani, "w:gz") as tf:
            info = tarfile.TarInfo("other.txt")
            data = b"x"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        assert pe.peek_pack_meta(nomani) is None

    def test_valid_manifest(self, tmp_path):
        from app.ops import pack_export as pe

        p = tmp_path / "aether-update-2.0.0.tar.gz"
        with tarfile.open(p, "w:gz") as tf:
            data = json.dumps({"version": "2.0.0", "min_compatible": "1.0.0",
                               "notes": "n"}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        meta = pe.peek_pack_meta(p)
        assert meta == {"version": "2.0.0", "min_compatible": "1.0.0", "notes": "n"}


class TestApplyLocalPack:
    def test_missing_pack_404(self, pe_iso):
        from app.core.exceptions import AppException

        pe, _, _ = pe_iso
        with pytest.raises(AppException) as ei:
            asyncio.run(pe.apply_local_pack("aether-update-9.9.9.tar.gz", "t"))
        assert ei.value.http_status == 404

    def test_apply_success_deletes_pack_and_audits(self, pe_iso, monkeypatch, audit_tmp):
        pe, _, pack_dir = pe_iso
        pack_dir.mkdir(parents=True)
        pack = pack_dir / "aether-update-1.2.0.tar.gz"
        pack.write_bytes(b"pack")

        async def fake_apply_upgrade(path, operator):
            assert path == pack and operator == "tester"
            return {"to_version": "1.2.0", "restarting": True}

        monkeypatch.setattr(pe.upgrade, "apply_upgrade", fake_apply_upgrade)
        result = asyncio.run(pe.apply_local_pack("aether-update-1.2.0.tar.gz", "tester"))
        assert result["to_version"] == "1.2.0"
        assert not pack.exists()  # 装完即删
        audits = _audit_lines(audit_tmp)
        assert audits[-1]["action"] == "pack_install"
        assert audits[-1]["detail"] == {"pack": "aether-update-1.2.0.tar.gz",
                                        "to_version": "1.2.0"}

    def test_apply_unlink_failure_still_returns(self, pe_iso, monkeypatch, audit_tmp):
        pe, _, pack_dir = pe_iso
        pack_dir.mkdir(parents=True)
        pack = pack_dir / "aether-update-1.2.1.tar.gz"
        pack.write_bytes(b"pack")

        async def fake_apply_upgrade(path, operator):
            return {"to_version": "1.2.1"}

        monkeypatch.setattr(pe.upgrade, "apply_upgrade", fake_apply_upgrade)
        if sys.platform == "win32":
            # Windows：保持打开句柄（无 FILE_SHARE_DELETE）使 unlink 报 PermissionError
            with open(pack, "rb") as held:
                result = asyncio.run(pe.apply_local_pack("aether-update-1.2.1.tar.gz", "t"))
                assert result["to_version"] == "1.2.1"
                assert pack.exists()  # 删包失败只告警，不阻断
        else:
            pack.chmod(0o400)
            try:
                result = asyncio.run(pe.apply_local_pack("aether-update-1.2.1.tar.gz", "t"))
                assert result["to_version"] == "1.2.1"
            finally:
                pack.chmod(0o600)


# ==================== app/ops/upgrade ====================


def _make_upgrade_pack(tmp_path: Path, manifest: dict, image_payload: bytes | None,
                       image_member: str = "images/aether.tar",
                       image_is_dir: bool = False) -> Path:
    pack = tmp_path / "aether-update-1.0.0.tar.gz"
    mdata = json.dumps(manifest).encode()
    with tarfile.open(pack, "w:gz") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(mdata)
        tf.addfile(info, io.BytesIO(mdata))
        if image_is_dir:
            dinfo = tarfile.TarInfo(image_member)
            dinfo.type = tarfile.DIRTYPE
            tf.addfile(dinfo)
        elif image_payload is not None:
            iinfo = tarfile.TarInfo(image_member)
            iinfo.size = len(image_payload)
            tf.addfile(iinfo, io.BytesIO(image_payload))
    return pack


class TestVerifyPack:
    def test_oversize_rejected(self, tmp_path, monkeypatch):
        from app.ops import upgrade as up

        big = tmp_path / "aether-update-1.0.0.tar.gz"
        big.write_bytes(b"x" * 100)
        monkeypatch.setattr(up, "MAX_PACK_BYTES", 10)
        with pytest.raises(ValueError, match="4GB"):
            up.verify_pack(big)

    def test_manifest_without_sha_rejected(self, tmp_path):
        from app.ops import upgrade as up

        pack = _make_upgrade_pack(
            tmp_path, {"version": "1.0.0", "images": [{"name": "aether-app"}]}, b"data")
        with pytest.raises(ValueError, match="校验信息"):
            up.verify_pack(pack)

    def test_bad_image_path_rejected(self, tmp_path):
        from app.ops import upgrade as up

        for bad in ("/abs/images/aether.tar", "images/../evil.tar"):
            pack = _make_upgrade_pack(
                tmp_path,
                {"version": "1.0.0", "images": [{"sha256": "x", "file": bad}]},
                b"data")
            with pytest.raises(ValueError, match="路径非法"):
                up.verify_pack(pack)

    def test_missing_image_member_rejected(self, tmp_path):
        from app.ops import upgrade as up

        pack = _make_upgrade_pack(
            tmp_path,
            {"version": "1.0.0", "images": [{"sha256": "x", "file": "images/aether.tar"}]},
            None, image_is_dir=True)
        with pytest.raises(ValueError, match="缺镜像文件"):
            up.verify_pack(pack)

    def test_valid_pack_and_version_gate(self, tmp_path, monkeypatch):
        from app.ops import upgrade as up

        payload = b"image-bytes"
        sha = hashlib.sha256(payload).hexdigest()
        pack = _make_upgrade_pack(
            tmp_path,
            {"version": "1.1.0", "min_compatible": "1.0.0",
             "images": [{"sha256": sha, "file": "images/aether.tar"}]},
            payload)
        monkeypatch.setattr(up, "get_version", lambda: "1.0.5")
        manifest = up.verify_pack(pack)
        assert manifest["version"] == "1.1.0"

        monkeypatch.setattr(up, "get_version", lambda: "0.9.0")  # 当前版本过低
        with pytest.raises(ValueError, match="最低兼容"):
            up.verify_pack(pack)

    def test_sha_mismatch_rejected(self, tmp_path):
        from app.ops import upgrade as up

        pack = _make_upgrade_pack(
            tmp_path,
            {"version": "1.0.0",
             "images": [{"sha256": hashlib.sha256(b"other").hexdigest(),
                         "file": "images/aether.tar"}]},
            b"actual-bytes")
        with pytest.raises(ValueError, match="sha256 不匹配"):
            up.verify_pack(pack)

    def test_corrupt_structure_rejected(self, tmp_path):
        """gzip 损坏 → tarfile.TarError → 「结构不合法」包装。"""
        from app.ops import upgrade as up

        pack = tmp_path / "aether-update-1.0.0.tar.gz"
        pack.write_bytes(b"\x1f\x8b" + b"corrupt-garbage" * 8)
        with pytest.raises(ValueError, match="结构不合法"):
            up.verify_pack(pack)


class TestDockerHelper:
    def test_missing_sock_raises(self, tmp_path, monkeypatch):
        from app.ops import upgrade as up

        monkeypatch.setattr(up, "DOCKER_SOCK", tmp_path / "nope.sock")
        with pytest.raises(RuntimeError, match="docker.sock"):
            asyncio.run(up._docker("GET", "/images/json"))

    def test_request_over_sock(self, tmp_path, monkeypatch):
        from app.ops import upgrade as up

        sock = tmp_path / "docker.sock"
        sock.write_bytes(b"")
        monkeypatch.setattr(up, "DOCKER_SOCK", sock)
        seen = {}

        class Client:
            def __init__(self, transport=None, timeout=None):
                seen["timeout"] = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, url, **kw):
                seen["method"], seen["url"], seen["kw"] = method, url, kw
                return _FakeResp(201, {"ok": True})

        monkeypatch.setattr(up.httpx, "AsyncClient", Client)
        resp = asyncio.run(up._docker("POST", "/images/load", timeout=30.0,
                                      params={"quiet": 1}))
        assert resp.status_code == 201 and resp.json() == {"ok": True}
        assert seen["method"] == "POST" and seen["url"] == "http://localhost/images/load"
        assert seen["kw"]["params"] == {"quiet": 1} and seen["timeout"] == 30.0


class TestApplyUpgrade:
    @pytest.fixture
    def up_iso(self, tmp_path, monkeypatch, audit_tmp):
        from app.ops import upgrade as up

        monkeypatch.setattr(up, "HISTORY_FILE", tmp_path / "hist" / "upgrade-history.jsonl")
        monkeypatch.setattr(up, "get_version", lambda: "1.0.0")
        return up, tmp_path

    def _patch_docker(self, monkeypatch, up, load_status=200, tag_status=201):
        calls = []

        async def fake_docker(method, path, timeout=300.0, **kw):
            calls.append((method, path, kw))
            if "/load" in path:
                return _FakeResp(load_status, text="load err")
            if "/tag" in path:
                return _FakeResp(tag_status, text="tag err")
            return _FakeResp(200)

        monkeypatch.setattr(up, "_docker", fake_docker)
        return calls

    def test_apply_success_schedules_restart(self, up_iso, monkeypatch, audit_tmp):
        up, tmp_path = up_iso
        calls = self._patch_docker(monkeypatch, up)

        started = {}

        class FakeTimer:
            def __init__(self, interval, fn):
                started["interval"], started["fn"] = interval, fn

            def start(self):
                started["started"] = True

        monkeypatch.setattr(up.threading, "Timer", FakeTimer)
        monkeypatch.setattr(up, "verify_pack",
                            lambda p: {"version": "1.2.0", "notes": "n"})
        pack = tmp_path / "aether-update-1.2.0.tar.gz"
        pack.write_bytes(b"pack")

        result = asyncio.run(up.apply_upgrade(pack, "tester"))
        assert result == {"from_version": "1.0.0", "to_version": "1.2.0",
                          "notes": "n", "restarting": True}
        assert [c[:2] for c in calls] == [("POST", "/images/load"),
                                          ("POST", "/images/aether-app:1.2.0/tag")]
        # load 走异步流式 content（同步文件对象会报错）：真实消费生成器体
        chunks = asyncio.run(_consume(calls[0][2]["content"]))
        assert chunks == [b"pack"]
        assert started["started"] is True and started["interval"] == 4.0
        # 历史与审计落盘
        hist = json.loads(up.HISTORY_FILE.read_text(encoding="utf-8").splitlines()[0])
        assert hist["to_version"] == "1.2.0" and hist["operator"] == "tester"
        audits = _audit_lines(audit_tmp)
        assert audits[-1]["action"] == "upgrade_apply"
        assert audits[-1]["detail"] == {"from_version": "1.0.0", "to_version": "1.2.0"}
        # 触发延迟重启回调（覆盖 restart 线程体）
        started["fn"]()
        assert ("POST", "/containers/aether/restart") == (calls[-1][0], calls[-1][1])

    def _apply(self, monkeypatch, up, tmp_path, fake_docker):
        started = {}

        class FakeTimer:
            def __init__(self, interval, fn):
                started["interval"], started["fn"] = interval, fn

            def start(self):
                started["started"] = True

        monkeypatch.setattr(up.threading, "Timer", FakeTimer)
        monkeypatch.setattr(up, "_docker", fake_docker)
        monkeypatch.setattr(up, "verify_pack", lambda p: {"version": "1.2.0"})
        pack = tmp_path / "pack.tar.gz"
        pack.write_bytes(b"p")
        result = asyncio.run(up.apply_upgrade(pack, "t"))
        assert result["restarting"] is True
        return started

    def test_restart_thread_tolerates_docker_failure(self, up_iso, monkeypatch, audit_tmp):
        """重启线程内 docker restart 抛错只记日志（覆盖内层 except）。"""
        up, tmp_path = up_iso

        async def fake_docker(method, path, timeout=300.0, **kw):
            if "/containers" in path:
                raise RuntimeError("sock gone")
            if "/load" in path:
                return _FakeResp(200)
            return _FakeResp(201)

        started = self._apply(monkeypatch, up, tmp_path, fake_docker)
        started["fn"]()  # 不向上抛
        assert started["interval"] == 4.0

    def test_restart_thread_tolerates_loop_failure(self, up_iso, monkeypatch, audit_tmp):
        """重启线程里新事件循环创建失败也不影响主流程（覆盖外层 except）。"""
        up, tmp_path = up_iso

        async def fake_docker(method, path, timeout=300.0, **kw):
            if "/load" in path:
                return _FakeResp(200)
            return _FakeResp(201)

        def _boom():
            raise RuntimeError("no loop")

        monkeypatch.setattr(asyncio, "new_event_loop", _boom)
        started = self._apply(monkeypatch, up, tmp_path, fake_docker)
        started["fn"]()  # 外层 except 吞掉
        assert started["interval"] == 4.0

    def test_load_failure_raises(self, up_iso, monkeypatch):
        up, tmp_path = up_iso
        self._patch_docker(monkeypatch, up, load_status=500)
        monkeypatch.setattr(up, "verify_pack", lambda p: {"version": "1.2.0"})
        pack = tmp_path / "pack.tar.gz"
        pack.write_bytes(b"p")
        with pytest.raises(RuntimeError, match="docker load 失败"):
            asyncio.run(up.apply_upgrade(pack, "t"))

    def test_tag_failure_raises(self, up_iso, monkeypatch):
        up, tmp_path = up_iso
        self._patch_docker(monkeypatch, up, tag_status=500)
        monkeypatch.setattr(up, "verify_pack", lambda p: {"version": "1.2.0"})
        pack = tmp_path / "pack.tar.gz"
        pack.write_bytes(b"p")
        with pytest.raises(RuntimeError, match="docker tag 失败"):
            asyncio.run(up.apply_upgrade(pack, "t"))


class TestUpgradeHistory:
    def test_missing_file_empty(self, tmp_path, monkeypatch):
        from app.ops import upgrade as up

        monkeypatch.setattr(up, "HISTORY_FILE", tmp_path / "none.jsonl")
        assert up.upgrade_history() == []

    def test_bad_lines_skipped_and_limit(self, tmp_path, monkeypatch):
        from app.ops import upgrade as up

        f = tmp_path / "upgrade-history.jsonl"
        rows = [{"to_version": str(i)} for i in range(5)]
        # 坏行放末尾（默认 limit=10 时仍在读取窗口内）
        f.write_text("\n".join([json.dumps(r) for r in rows] + ["{broken"]),
                     encoding="utf-8")
        monkeypatch.setattr(up, "HISTORY_FILE", f)
        hist = up.upgrade_history()
        assert [h["to_version"] for h in hist] == ["4", "3", "2", "1", "0"]  # 坏行被跳过

    def test_unreadable_file_returns_empty(self, tmp_path, monkeypatch):
        from app.ops import upgrade as up

        d = tmp_path / "as-dir.jsonl"
        d.mkdir()  # exists() 为 True 但 read_text 抛 OSError（目录）
        monkeypatch.setattr(up, "HISTORY_FILE", d)
        assert up.upgrade_history() == []
