"""容器降权后的可写路径契约（管理页保存配置 500 的回归防护）。

根因回顾：容器降权（commit 0c14ca6）后进程以 aether(uid 10001) 运行，
但镜像里 /aether 目录属 root——atomic_write 要在 /aether 下落
config.json.tmp、_safe_backup_config 落 config.json.bak、write_secrets
写 /aether/.env，目录不可写时所有配置保存一律 PermissionError → 500。

修复契约（缺一不可，这里用脚本/镜像内容断言守卫）：
- scripts/entrypoint_tls.sh root 装配段：chown /aether 目录本身给应用用户，
  且发生在 gosu 降权之前；
- Dockerfile 构建期：chown aether:aether /aether（只目录本身，不递归——
  应用代码保持 root 属主，进程不可改写自身代码）。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_entrypoint_chowns_aether_dir_itself_before_gosu():
    script = (PROJECT_ROOT / "scripts" / "entrypoint_tls.sh").read_text(encoding="utf-8")
    # 精确匹配整行（/aether 后不带 /），防止只匹配到 /aether/config.json 单文件行
    chown_pattern = re.compile(
        r'^[ \t]*chown "\$APP_USER:\$APP_USER" /aether 2>/dev/null \|\| true$',
        re.MULTILINE,
    )
    matches = list(chown_pattern.finditer(script))
    assert matches, "entrypoint 缺少对 /aether 目录本身的 chown（保存配置会 500）"

    # chown 必须以 root 身份执行（在最终 exec gosu 降权之前）
    final_gosu = script.rfind("exec gosu")
    assert final_gosu != -1, "entrypoint 应以 gosu 降权运行"
    assert matches[0].start() < final_gosu, "/aether 的 chown 必须在 gosu 降权之前（root 装配段内）"


def test_dockerfile_chowns_aether_dir_at_build_time():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # 只目录本身（行尾即结束），与递归 chown 各挂载子目录的行区分开
    assert re.search(
        r"^\s*&&\s+chown aether:aether /aether$",
        dockerfile,
        re.MULTILINE,
    ), "Dockerfile 缺少 chown aether:aether /aether（目录本身，非递归）"
