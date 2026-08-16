"""git 一键升级测试 — /api/ops/update/git/* 的服务层与路由。

覆盖：
- 令牌配置读写（config 隔离；GET 语义只回 configured 由路由保证）
- 仓库路径探测：config 覆盖优先；label 探测回退；label 缺失报引导错误
- 升级容器 payload：挂载（仓库 rw / docker.sock / logs）、env 注入、host-gateway
- 结果文件解析与日志尾读取
- 路由注册（git 四端点挂上 /api）
Docker 真实链路（拉容器跑 run.sh）属部署侧人工验收，见设计文档。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import AppException
from app.ops import git_update as gu


# ==================== 令牌配置 ====================

class TestGitToken:
    def test_set_and_get_roundtrip(self, tmp_path, monkeypatch):
        import app.core.config as cfg
        # 直接操作全局 CONFIG（get/update_config_section 都走它），
        # 结束由 monkeypatch 恢复，测试不落盘（CONFIG_PATH 指到临时文件）
        cfg.CONFIG.setdefault("update", {})
        monkeypatch.setattr(cfg, "CONFIG_PATH", tmp_path / "config.json")
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        assert gu.set_git_token("  tok123  ") is True
        assert gu.get_git_token() == "tok123"
        assert gu.set_git_token("") is False
        assert gu.get_git_token() == ""


# ==================== 仓库路径探测 ====================

class TestResolveRepoPath:
    def test_config_override_wins(self, monkeypatch):
        monkeypatch.setattr(gu, "get_repo_path_cached", lambda: "/srv/aether")
        assert asyncio_run(gu.resolve_repo_path()) == "/srv/aether"

    def test_label_probe(self, monkeypatch):
        monkeypatch.setattr(gu, "get_repo_path_cached", lambda: "")
        monkeypatch.setattr(gu.upgrade, "DOCKER_SOCK", type("P", (), {"exists": lambda s: True})())
        resp = type("R", (), {
            "status_code": 200,
            "json": lambda s: {"Config": {"Labels": {
                "com.docker.compose.project.working_dir": "/home/pi/Aether"}}},
        })()
        monkeypatch.setattr(gu, "_docker", AsyncMock(return_value=resp))
        assert asyncio_run(gu.resolve_repo_path()) == "/home/pi/Aether"

    def test_missing_label_raises_guided_error(self, monkeypatch):
        monkeypatch.setattr(gu, "get_repo_path_cached", lambda: "")
        monkeypatch.setattr(gu.upgrade, "DOCKER_SOCK", type("P", (), {"exists": lambda s: True})())
        resp = type("R", (), {
            "status_code": 200,
            "json": lambda s: {"Config": {"Labels": {}}},
        })()
        monkeypatch.setattr(gu, "_docker", AsyncMock(return_value=resp))
        with pytest.raises(AppException, match="仓库"):
            asyncio_run(gu.resolve_repo_path())


# ==================== 容器 payload ====================

class TestContainerPayload:
    def test_binds_env_and_gateway(self):
        p = gu._container_payload("apply", "/repo/path", "tok")
        binds = p["HostConfig"]["Binds"]
        assert "/repo/path:/repo" in binds
        assert "/var/run/docker.sock:/var/run/docker.sock" in binds
        assert "/repo/path/logs:/result" in binds   # 结果卷=宿主仓库下的 logs/
        assert p["HostConfig"]["ExtraHosts"] == ["host.docker.internal:host-gateway"]
        env = dict(e.split("=", 1) for e in p["Env"])
        assert env["MODE"] == "apply"
        assert env["GIT_TOKEN"] == "tok"
        assert env["RESULT_FILE"] == "/result/git-update-result.json"

    def test_windows_backslash_path_normalized(self):
        """Windows 探测出的反斜杠路径必须转正斜杠（Docker bind 源不接受 \\）。"""
        p = gu._container_payload("check", "D:\\Aether", "t")
        binds = p["HostConfig"]["Binds"]
        assert "D:/Aether:/repo" in binds
        assert "D:/Aether/logs:/result" in binds

    def test_check_mode(self):
        p = gu._container_payload("check", "/r", "t")
        env = dict(e.split("=", 1) for e in p["Env"])
        assert env["MODE"] == "check"
        assert p["Labels"] == {"aether.git-update": "check"}


# ==================== 结果与日志 ====================

class TestResultFiles:
    def test_read_result_parses_json(self, tmp_path, monkeypatch):
        f = tmp_path / "r.json"
        f.write_text(json.dumps({"status": "available", "behind": 3}), encoding="utf-8")
        monkeypatch.setattr(gu, "RESULT_FILE", f)
        assert gu.read_result()["behind"] == 3

    def test_read_result_missing_or_corrupt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gu, "RESULT_FILE", tmp_path / "nope.json")
        assert gu.read_result() is None
        bad = tmp_path / "bad.json"
        bad.write_text("{broken", encoding="utf-8")
        monkeypatch.setattr(gu, "RESULT_FILE", bad)
        assert gu.read_result() is None

    def test_log_tail_limits_lines(self, tmp_path, monkeypatch):
        f = tmp_path / "log"
        f.write_text("\n".join(f"line{i}" for i in range(500)), encoding="utf-8")
        monkeypatch.setattr(gu, "LOG_FILE", f)
        tail = gu.read_log_tail(lines=10)
        assert tail.splitlines()[-1] == "line499"
        assert len(tail.splitlines()) == 10

    def test_status_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gu, "RESULT_FILE", tmp_path / "nope.json")
        monkeypatch.setattr(gu, "LOG_FILE", tmp_path / "nope.log")
        monkeypatch.setattr(gu, "get_git_token", lambda: "x")
        s = asyncio_run(gu.status_git_update())
        assert s == {"result": None, "log_tail": "", "token_configured": True}


# ==================== 路由注册 ====================

def test_git_routes_registered():
    import app.main as m
    paths = {r.path for r in m.app.routes if hasattr(r, "path")}
    assert "/api/ops/update/git" in paths
    assert "/api/ops/update/git/check" in paths
    assert "/api/ops/update/git/apply" in paths
    assert "/api/ops/update/git/status" in paths


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
