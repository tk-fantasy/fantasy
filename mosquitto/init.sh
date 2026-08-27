#!/bin/sh
# mosquitto 容器启动入口：首次启动自动生成 passwd 文件。
#
# passwd 文件被 .gitignore 排除（含哈希密码，不该进版本库），新用户 clone 后
# 没有 passwd 文件，mosquitto 启动会报 "Unable to open pwfile" 退出。本脚本
# 检测 passwd 不存在时用 mosquitto_passwd 生成。
#
# 凭证经环境变量注入（compose 从宿主 .env 读取 MQTT_USER/MQTT_PASSWORD，
# 未配置时默认 aether/aether），与 ha_simulator.py / add_mqtt_config.py 的
# 读取逻辑一致。已存在 passwd（如用户自己改过密码）则跳过，幂等——改密码
# 请用 mosquitto_passwd 重新生成后重启容器，而不是删 passwd 回默认值。

PASSWD_FILE="/mosquitto/config/passwd"
MQTT_USER="${MQTT_USER:-aether}"
MQTT_PASS="${MQTT_PASSWORD:-aether}"

# 弱口令告警：不阻断启动（改密码需宿主 .env + compose 一起改，容器内无法
# 单方面轮换——其他容器按同一环境变量取密码，单方面改会造成互相失联），
# 但要在日志里喊出来，提醒去 .env 设置强 MQTT_PASSWORD。
if [ "$MQTT_PASS" = "aether" ] || [ ${#MQTT_PASS} -lt 8 ]; then
    echo "[init] ============================================================"
    echo "[init] 警告: MQTT_PASSWORD 使用默认/弱口令（长度 ${#MQTT_PASS}）"
    echo "[init] 宿主端口已仅绑定 127.0.0.1，风险已收窄；仍建议在宿主 .env"
    echo "[init] 中设置 8 位以上强密码后 docker compose up -d 重建生效"
    echo "[init] ============================================================"
fi

if [ ! -f "$PASSWD_FILE" ]; then
    echo "[init] passwd 文件不存在，生成默认凭证 $MQTT_USER/******"
    # -b: 命令行传密码（非交互）；-c: 创建新文件
    mosquitto_passwd -b -c "$PASSWD_FILE" "$MQTT_USER" "$MQTT_PASS"
    chmod 644 "$PASSWD_FILE"
    echo "[init] passwd 已生成: $PASSWD_FILE"
else
    echo "[init] passwd 已存在，跳过生成"
fi

# 启动 mosquitto（前台运行，容器主进程）
exec mosquitto -c /mosquitto/config/mosquitto.conf
