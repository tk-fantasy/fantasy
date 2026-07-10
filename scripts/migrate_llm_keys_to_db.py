"""迁移脚本：把 config.json 的 llm_keys 和 providers 迁移到 DB per-user。

运行方式：
    python scripts/migrate_llm_keys_to_db.py

逻辑：
    1. 读取 config.json 中的 llm_keys 和 providers
    2. 读取 .env 中的 API keys
    3. 找到第一个用户（通常是 admin）
    4. 把 llm_keys（含实际 API key）存入该用户的 user_settings
    5. 把 providers 存入该用户的 user_settings
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import CONFIG_PATH, get_config
from app.core.database import Database


def load_env_file() -> dict[str, str]:
    """从 .env 文件读取环境变量。"""
    env_path = CONFIG_PATH.parent / ".env"
    env_vars = {}
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    env_vars[key] = value
    return env_vars


async def migrate():
    """执行迁移。"""
    print("=" * 60)
    print("LLM Keys 迁移脚本")
    print("=" * 60)

    # 初始化数据库
    await Database.init()
    db = Database.get()

    # 获取用户列表
    users = await db.user_list_all()
    if not users:
        print("❌ 没有找到任何用户，请先注册一个用户")
        return

    print(f"\n找到 {len(users)} 个用户:")
    for u in users:
        print(f"  - {u['username']} ({u['display_name'] or '无显示名'})")

    # 使用第一个用户
    target_user = users[0]
    target_user_id = target_user["id"]
    print(f"\n将迁移到用户: {target_user['username']}")

    # 读取 config.json 中的 llm_keys
    file_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    llm_keys = file_config.get("llm_keys", [])
    providers = file_config.get("providers", {})

    print(f"\n从 config.json 读取:")
    print(f"  - llm_keys: {len(llm_keys)} 个")
    print(f"  - providers: {list(providers.keys())}")

    if not llm_keys:
        print("\n⚠️ config.json 中没有 llm_keys，跳过迁移")
        return

    # 读取 .env 中的 API keys
    env_vars = load_env_file()
    print(f"  - .env 变量: {len(env_vars)} 个")

    # 为每个 key 填充实际的 API key
    for key in llm_keys:
        env_name = key.get("api_key_env", "")
        if env_name and env_name in env_vars:
            key["api_key"] = env_vars[env_name]
            print(f"  - 填充 {key.get('id')}: {env_name} -> {key['api_key'][:10]}...")

    # 检查用户是否已有 llm_keys
    existing_json = await db.user_setting_get(target_user_id, "llm_keys")
    if existing_json:
        existing = json.loads(existing_json)
        if existing:
            print(f"\n⚠️ 用户 {target_user['username']} 已有 {len(existing)} 个 llm_keys")
            response = input("是否覆盖? (y/N): ").strip().lower()
            if response != "y":
                print("取消迁移")
                return

    # 保存到 DB
    await db.user_setting_set(
        target_user_id,
        "llm_keys",
        json.dumps(llm_keys, ensure_ascii=False)
    )
    print(f"\n✅ 已保存 {len(llm_keys)} 个 llm_keys 到 DB")

    if providers:
        await db.user_setting_set(
            target_user_id,
            "providers",
            json.dumps(providers, ensure_ascii=False)
        )
        print(f"✅ 已保存 providers 到 DB")

    # 验证
    saved_json = await db.user_setting_get(target_user_id, "llm_keys")
    saved = json.loads(saved_json) if saved_json else []
    print(f"\n验证: DB 中有 {len(saved)} 个 llm_keys")

    for key in saved:
        has_key = bool(key.get("api_key", ""))
        print(f"  - {key.get('id')}: {key.get('type')} / {key.get('model')} (API key: {'✅' if has_key else '❌'})")

    print("\n" + "=" * 60)
    print("迁移完成!")
    print("=" * 60)
    print("\n注意:")
    print("  - config.json 中的 llm_keys 仍然保留（作为备份）")
    print("  - 切换用户时会从 DB 加载该用户的配置")
    print("  - 可以安全地删除 config.json 中的 llm_keys（可选）")


if __name__ == "__main__":
    asyncio.run(migrate())
