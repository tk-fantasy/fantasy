"""CopyTruncateRotatingFileHandler 测试。

核心场景：os.rename 被环境拒绝（Docker Desktop Windows bind-mount 下
宿主持有文件句柄时的真实表现）时轮转仍能完成——标准 RotatingFileHandler
在此场景文件日志停写（线上 app.log 冻结 10MB 的根因）。
"""
from __future__ import annotations

import logging
from unittest.mock import patch

from app.core.log_rotate import CopyTruncateRotatingFileHandler


def _make_handler(path, max_bytes=100, backup_count=2):
    h = CopyTruncateRotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    logger = logging.getLogger("test.copytruncate")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(h)
    return logger, h


class TestCopyTruncateRotation:
    def test_rotates_without_rename(self, tmp_path):
        """os.rename 全程抛 PermissionError（模拟共享层）时轮转依然成功。"""
        log = tmp_path / "app.log"
        with patch("os.rename", side_effect=PermissionError(13, "Permission denied")):
            logger, h = _make_handler(log)
            for i in range(20):
                logger.info("line-%03d %s", i, "x" * 20)  # 每条远超 maxBytes/10
            h.flush()
            h.close()

        content = log.read_text(encoding="utf-8")
        assert content, "主文件轮转后应继续写入（标准 handler 此场景停写）"
        assert (tmp_path / "app.log.1").exists(), "旧内容应被复制到 .1"
        assert "line-019" in content, "最新一条必须落在主文件"

    def test_backup_chain_copies(self, tmp_path):
        """多次轮转形成 .1/.2 备份链（复制而非 rename）。"""
        log = tmp_path / "app.log"
        logger, h = _make_handler(log, max_bytes=80, backup_count=2)
        for i in range(30):
            logger.info("gen-%d %s", i, "y" * 40)
        h.flush()
        h.close()

        assert (tmp_path / "app.log.1").exists()
        assert (tmp_path / "app.log.2").exists()
        # 链上新旧关系：.1 比主文件旧、比 .2 新
        c1 = (tmp_path / "app.log.1").read_text(encoding="utf-8")
        c2 = (tmp_path / "app.log.2").read_text(encoding="utf-8")
        main = log.read_text(encoding="utf-8")
        assert c1 and c2 and main
        gens_1 = {l.split()[0] for l in c1.splitlines() if l.startswith("gen-")}
        gens_2 = {l.split()[0] for l in c2.splitlines() if l.startswith("gen-")}
        gens_m = {l.split()[0] for l in main.splitlines() if l.startswith("gen-")}
        assert max(int(g.split("-")[1]) for g in gens_m) > max(int(g.split("-")[1]) for g in gens_1)
        assert max(int(g.split("-")[1]) for g in gens_1) > max(int(g.split("-")[1]) for g in gens_2)

    def test_no_rename_or_remove_calls(self, tmp_path):
        """轮转全程不调 os.rename / os.remove（共享层敏感操作零依赖）。"""
        log = tmp_path / "app.log"
        with patch("os.rename") as mock_rename, patch("os.remove") as mock_remove:
            logger, h = _make_handler(log)
            for i in range(10):
                logger.info("z" * 40)
            h.flush()
            h.close()
        mock_rename.assert_not_called()
        mock_remove.assert_not_called()
