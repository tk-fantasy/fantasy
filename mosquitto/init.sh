#!/bin/sh
# mosquitto 容器启动入口：首次启动自动生成 passwd 文件。
#
# passwd 文件被 .gitignore 排除（含哈希密码，不该进版本库），新用户 clone 后
# 没有 passwd 文件，mosquitto 启动会报 "Unable to open pwfile" 退出。本脚本
# 检测 passwd 不存在时用 mosquitto_passwd 生成（凭证固定 aether/aether，与
# ha_simulator.py / add_mqtt_config.py 用的凭证一致）。
#
# 已存在 passwd（如用户自己改过密码）则跳过，幂等。

PASSWD_FILE="/mosquitto/config/passwd"
MQTT_USER="aether"
MQTT_PASS="aether"

if [ ! -f "$PASSWD_FILE" ]; then
    echo "[init] passwd 文件不存在，生成默认凭证 $MQTT_USER/$MQTT_PASS"
    # -b: 命令行传密码（非交互）；-c: 创建新文件
    mosquitto_passwd -b -c "$PASSWD_FILE" "$MQTT_USER" "$MQTT_PASS"
    chmod 644 "$PASSWD_FILE"
    echo "[init] passwd 已生成: $PASSWD_FILE"
else
    echo "[init] passwd 已存在，跳过生成"
fi

# 启动 mosquitto（前台运行，容器主进程）
exec mosquitto -c /mosquitto/config/mosquitto.conf
