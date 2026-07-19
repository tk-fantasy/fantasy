"""为 Home Assistant 配置 MQTT broker 集成。

首次启动 HA 后，core.config_entries 里还没有 MQTT 集成（之前是 clone
ha_config 带过来的，现在 .storage 状态文件已不进版本库，新用户首次启动
HA 是干净的）。本脚本负责创建或更新 MQTT 集成，指向 docker compose 里的
mosquitto 服务（broker=mqtt, port=1884, user=aether, pass=aether）。

用法：
    cd ha_config && python add_mqtt_config.py
    （或在容器内：docker exec aether-ha python /config/add_mqtt_config.py）

幂等：已存在 MQTT 集成时只更新端口/凭证，不会重复创建。
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

config_file = Path(__file__).parent / ".storage" / "core.config_entries"

# MQTT broker 配置（与 docker-compose.yml 的 mosquitto 服务一致）
MQTT_BROKER = "mqtt"          # compose 服务名（容器内可解析）
MQTT_PORT = 1884              # mosquitto 容器内监听端口（宿主映射 1884→1884）
MQTT_USERNAME = "aether"
MQTT_PASSWORD = "aether"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_mqtt_entry() -> dict:
    """构造一条 MQTT config_entry（结构对齐 HA 2026.6 core.config_entries schema）。"""
    return {
        "created_at": _iso_now(),
        "data": {
            "broker": MQTT_BROKER,
            "port": MQTT_PORT,
            "protocol": "5",
            "username": MQTT_USERNAME,
            "password": MQTT_PASSWORD,
        },
        "disabled_by": None,
        "discovery_keys": {},
        "domain": "mqtt",
        "entry_id": uuid.uuid4().hex[:26].upper(),
        "minor_version": 1,
        "modified_at": _iso_now(),
        "options": {},
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "user",
        "subentries": [],
        "title": f"MQTT ({MQTT_BROKER}:{MQTT_PORT})",
        "unique_id": None,
        "version": 1,
    }


def main() -> None:
    if not config_file.exists():
        # core.config_entries 还没生成（HA 尚未完成首次启动），不能强行创建
        print(f"✗ {config_file} 不存在，请先完成 HA 首次启动（打开 8123 走 onboarding）后再运行本脚本。")
        return

    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data.setdefault("data", {}).setdefault("entries", [])

    mqtt_entry = next((e for e in entries if e.get("domain") == "mqtt"), None)

    if mqtt_entry is None:
        # 首次创建
        entries.append(_build_mqtt_entry())
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"✓ 已创建 MQTT 集成：broker={MQTT_BROKER} port={MQTT_PORT} user={MQTT_USERNAME}")
        print("  重启 HA 容器后生效：docker compose restart homeassistant")
        return

    # 已存在：更新 broker/port/凭证（若有变更）
    changed = []
    d = mqtt_entry.setdefault("data", {})
    if d.get("broker") != MQTT_BROKER:
        d["broker"] = MQTT_BROKER
        changed.append(f"broker→{MQTT_BROKER}")
    if d.get("port") != MQTT_PORT:
        d["port"] = MQTT_PORT
        changed.append(f"port→{MQTT_PORT}")
    if d.get("username") != MQTT_USERNAME:
        d["username"] = MQTT_USERNAME
        changed.append(f"username→{MQTT_USERNAME}")
    if d.get("password") != MQTT_PASSWORD:
        d["password"] = MQTT_PASSWORD
        changed.append(f"password→***")
    mqtt_entry["modified_at"] = _iso_now()

    if changed:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"✓ MQTT 集成已更新：{', '.join(changed)}")
        print("  重启 HA 容器后生效：docker compose restart homeassistant")
    else:
        print(f"ℹ MQTT 集成已存在且配置正确（broker={MQTT_BROKER} port={MQTT_PORT}）")


if __name__ == "__main__":
    main()
