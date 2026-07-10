"""旧配置迁移脚本 — 把 config.json 中的家庭信息迁移到第一个用户的 user_settings。"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_config
from app.core.database import Database

logger = logging.getLogger(__name__)


async def migrate():
    """执行迁移。"""
    # 初始化数据库
    await Database.init()
    db = Database.get()

    # 检查是否有用户
    user_count = await db.user_count()
    if user_count == 0:
        logger.info("No users found, skipping migration")
        return

    # 获取第一个用户
    async with db._db.execute("SELECT id, username FROM users ORDER BY created_at LIMIT 1") as cursor:
        row = await cursor.fetchone()
        if not row:
            logger.info("No users found, skipping migration")
            return
        user_id, username = row

    logger.info("Migrating config to user: %s (%s)", username, user_id)

    # 迁移家庭信息
    home_config = get_config("home", {})
    if home_config:
        home_data = {
            "home_name": home_config.get("home_name", ""),
            "owner_name": home_config.get("owner_name", ""),
            "province": home_config.get("province", ""),
            "city": home_config.get("city", ""),
            "district": home_config.get("district", ""),
        }
        # 检查是否已有家庭信息
        existing = await db.user_setting_get(user_id, "home_info")
        if not existing:
            await db.user_setting_set(user_id, "home_info", json.dumps(home_data, ensure_ascii=False))
            logger.info("Migrated home info: %s", home_data)
        else:
            logger.info("Home info already exists for user, skipping")

    logger.info("Migration completed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(migrate())
