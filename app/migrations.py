"""启动期一次性历史数据迁移。

把按 per-user 存在 DB 里的旧数据形状迁到 config.json（新真源）/ 新 KV。
从 ``main.py`` 的 lifespan 抽出，逐字保留原容错语义：单块失败只 warning，
不阻塞后续启动步骤。

依赖通过参数显式注入（``db`` / ``vision_service``），不触碰模块级全局状态，
便于单测隔离。
"""
from __future__ import annotations

import json
import logging

from .core.config import (
    get_config,
    save_global_llm_keys,
    update_config_section,
    update_memory_config,
)

logger = logging.getLogger(__name__)


async def migrate_global_llm_keys(db) -> None:
    """加载全局 LLM keys：优先 config.json（全局共享、跨重启持久化）。

    仅当 config.json 的 llm_keys 为空时，fallback 到"第一个有 llm_keys 的用户 DB"，
    并一次性迁移到 config.json（兼容历史部署：老版本把全局 key 只存用户 DB）。
    """
    # 加载全局 LLM keys：优先 config.json（全局共享、跨重启持久化）；
    # 仅当 config.json 的 llm_keys 为空时，fallback 到"第一个有 llm_keys 的用户 DB"，
    # 并一次性迁移到 config.json（兼容历史部署：老版本把全局 key 只存用户 DB）。
    try:
        config_keys = get_config("llm_keys", []) or []
        if config_keys:
            logger.info("Loaded %d global LLM keys from config.json", len(config_keys))
        else:
            # config.json 无 key：fallback 到第一个有 llm_keys 的用户 DB，并迁移到 config.json
            all_users = await db.user_list_all()
            migrated = False
            for user in all_users:
                llm_keys_json = await db.user_setting_get(user["id"], "llm_keys")
                if not llm_keys_json:
                    continue
                llm_keys = json.loads(llm_keys_json)
                if not llm_keys:
                    continue
                update_memory_config("llm_keys", llm_keys)
                # 同步 providers（老版本把第一个用户的 providers 当全局用，迁移时一并持久化）
                providers_json = await db.user_setting_get(user["id"], "providers")
                migrated_providers: dict = {}
                if providers_json:
                    providers = json.loads(providers_json)
                    if providers:
                        update_memory_config("providers", providers)
                        migrated_providers = providers
                # 一次性迁移到 config.json 持久化（llm_keys 数组 + providers dict）
                try:
                    save_global_llm_keys(llm_keys)
                    if migrated_providers:
                        update_config_section("providers", migrated_providers)
                    migrated = True
                    logger.info(
                        "Migrated %d LLM keys + providers from user '%s' DB to config.json",
                        len(llm_keys), user["username"],
                    )
                except Exception:
                    logger.warning("Failed to persist migrated llm_keys to config.json", exc_info=True)
                break
            if not migrated:
                logger.info("No LLM keys found in config.json or any user DB")
    except Exception as e:
        logger.warning("Failed to load global LLM keys: %s", e)


async def migrate_home_info(db) -> None:
    """一次性迁移 home_info：把历史按 per-user 存进 DB 的家庭地址镜像到 config.json 的 home 段。

    历史代码把家庭地址按 per-user 存进了 DB（user_settings.home_info），但
    weather_service.get_weather() 读的是全局 config.json 的 home 段，两边不通导致天气
    组件空白。仅当 config 没有完整 home 时迁移（DB 仍是兼容性 fallback，config.json 是新真源）。
    """
    try:
        home_config = get_config("home", {}) or {}
        home_complete = bool(home_config.get("city") or home_config.get("district"))
        if not home_complete:
            all_users = await db.user_list_all()
            for user in all_users:
                home_info_json = await db.user_setting_get(user["id"], "home_info")
                if not home_info_json:
                    continue
                try:
                    home_data = json.loads(home_info_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not (home_data.get("city") or home_data.get("district")):
                    continue
                update_config_section("home", {
                    "home_name": home_data.get("home_name", ""),
                    "owner_name": home_data.get("owner_name", ""),
                    "province": home_data.get("province", ""),
                    "city": home_data.get("city", ""),
                    "district": home_data.get("district", ""),
                })
                logger.info(
                    "Migrated home_info from user '%s' DB to config.json (city=%s)",
                    user["username"], home_data.get("city"),
                )
                break
    except Exception as e:
        logger.warning("Failed to migrate home_info from user DB to config.json: %s", e)


async def load_vision_focuses(db, vision_service) -> None:
    """加载视觉关注指令（支持新多条格式 + 旧单条迁移）。

    KV 有 ``vision_focuses`` 时直接 load；否则若存在旧单条 ``vision_focus``，迁成新多条
    格式并回写 KV。
    """
    # 加载视觉关注指令（支持新多条格式 + 旧单条迁移）
    saved_focuses = await db.kv_get("vision_focuses")
    if saved_focuses:
        try:
            focuses = json.loads(saved_focuses)
            vision_service.load_focuses(focuses)
            logger.info("Loaded %d vision_focuses from database", len(focuses))
        except (ValueError, TypeError):
            logger.warning("Failed to parse vision_focuses from database")
    else:
        # 迁移旧 vision_focus
        saved_focus = await db.kv_get("vision_focus")
        if saved_focus:
            vision_service.add_focus(saved_focus)
            await db.kv_set("vision_focuses", json.dumps(vision_service.get_vision_focuses()))
            logger.info("Migrated old vision_focus to new format: %s", saved_focus[:50])


async def migrate_camera_frame_interval(db) -> None:
    """cameras.frame_interval_ms 默认值演进：2000ms → 1000ms（3 帧覆盖约 2 秒）。

    旧默认的"2 秒一帧 × 3 帧"时间序列太稀，动态条件（如"正在坐下"）语义弱；
    新默认约 1 秒间隔让三帧更连续。只改写仍等于旧默认值的行，
    用户显式设置过的其他间隔原样保留。幂等：跑过后不再命中。
    """
    try:
        changed = await db.cameras_remap_frame_interval(old_ms=2000, new_ms=1000)
        if changed:
            logger.info("已将 %d 路摄像头的抓帧间隔从 2000ms 迁移到 1000ms", changed)
    except Exception:
        logger.warning("cameras.frame_interval_ms 默认值迁移失败（跳过，不影响启动）",
                       exc_info=True)
