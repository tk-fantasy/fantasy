"""
MQTT 虚拟设备模拟器
用法:
  python ha_simulator.py                  # 宿主机 (localhost:1884)
  python ha_simulator.py --docker         # Docker 内  (mqtt:1883)
  python ha_simulator.py --host X --port Y  # 自定义
"""
import os, sys

# Windows 控制台 UTF-8 编码修复
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        os.system("chcp 65001 >nul 2>&1")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import paho.mqtt.client as mqtt
import signal, json, argparse
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--docker", action="store_true")
parser.add_argument("--host", default=None)
parser.add_argument("--port", type=int, default=None)
args = parser.parse_args()

BROKER = args.host or ("mqtt" if args.docker else "localhost")
PORT = args.port or 1884
# MQTT 认证凭据（mosquitto 已关闭匿名连接）。
# 支持环境变量覆盖；默认与 mosquitto/config/passwd 中的 aether 用户一致。
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "aether")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "aether")
now = lambda: datetime.now().strftime("%H:%M:%S")
log = lambda msg: print(f"[{now()}] {msg}", flush=True)

# --- 单实例锁 (防止多个模拟器互相抢占导致状态跳动) ---
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".simulator.lock")

def _pid_alive(pid):
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            old_pid = int(open(LOCK_FILE).read().strip())
            if _pid_alive(old_pid):
                log(f"模拟器已在运行 (PID={old_pid})，退出")
                sys.exit(0)
        except (ValueError, OSError):
            pass
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

state = {
    "bedroom/light":      {"state": "OFF", "brightness": None},
    "kitchen/light":      {"state": "OFF", "brightness": None},
    "living_room/ceiling":{"state": "OFF", "brightness": None},
    "living_room/ac":     {"mode":"cool", "temp":24, "current_temp":26.0,
                           "fan":"auto", "swing":"off"},
    "living_room/curtain":{"position": 100},
    "living_room/fan":    {"state": "OFF", "speed": "low", "oscillation": False},
}

def pub(c, t, p):
    c.publish(t, p)
    log(f"→ {t}: {p}")

# ---------------------------------------------------------------
# 设备发布格式：HA 对不同 domain 期望不同的 topic 结构
# ---------------------------------------------------------------
def _publish_device(c, device_key):
    """根据 state 结构自动推断发布格式。"""
    s = state[device_key]

    # Climate：多属性独立 topic
    if "mode" in s and "temp" in s:
        for attr in ("mode", "temp", "fan", "swing"):
            if attr in s:
                pub(c, f"{device_key}/{attr}", str(s[attr]))
        if "current_temp" in s:
            pub(c, f"{device_key}/current_temp", str(s["current_temp"]))
        return

    # Cover：位置数值 + open/closed 文本
    if "position" in s:
        pos = s["position"]
        pub(c, f"{device_key}/position", str(pos))
        pub(c, device_key, "open" if pos > 0 else "closed")
        return

    # Fan：JSON 主体 + 子属性 topic
    if any(k in s for k in ("oscillation", "speed", "percentage")):
        pub(c, device_key, json.dumps(s))
        for attr in ("speed", "oscillation", "percentage", "preset_mode"):
            if attr in s:
                val = s[attr]
                if isinstance(val, bool):
                    val = "ON" if val else "OFF"
                pub(c, f"{device_key}/{attr}", str(val))
        return

    # Light / 默认：JSON 全量
    data = {}
    for k, v in s.items():
        if v is not None:
            data[k] = v
    pub(c, device_key, json.dumps(data))

# ---------------------------------------------------------------
# 自动路由：根据 MQTT topic 定位设备并更新状态
# ---------------------------------------------------------------
def _resolve_device(topic):
    """从 topic 自动匹配 state 中的设备 key（最长前缀匹配）。"""
    path = topic[:-4] if topic.endswith("/set") else topic  # 去掉 /set 后缀
    parts = path.split("/")
    for n in range(len(parts), 0, -1):
        key = "/".join(parts[:n])
        if key in state:
            return key, parts[n:]
    return None, []


def _coerce(value, target_type):
    """根据目标属性的当前类型自动转换值。"""
    if value in ("ON", "OFF") and isinstance(target_type, str):
        v = value
    elif isinstance(target_type, bool):
        v = value in ("ON", "true", "True", "1")
    elif isinstance(target_type, int):
        v = int(float(value))
    elif isinstance(target_type, float):
        v = float(value)
    else:
        v = value
    return v


def handle_set(c, topic, raw):
    if not topic.endswith("/set"):
        return

    device_key, attr_segments = _resolve_device(topic)
    if device_key is None:
        return

    device_state = state[device_key]

    if not attr_segments:
        # 设备级: room/device/set → 尝试 JSON，回退文本
        try:
            d = json.loads(raw)
            for k, v in d.items():
                if k in device_state:
                    device_state[k] = _coerce(v, type(device_state[k]) if device_state[k] is not None else str)
            log(f"← [{device_key}] {d}")
        except (json.JSONDecodeError, ValueError):
            # 纯文本命令：ON/OFF/OPEN/CLOSE/STOP
            if raw in ("ON", "OFF"):
                device_state["state"] = raw
            elif raw in ("OPEN", "CLOSE", "STOP") and "position" in device_state:
                device_state["position"] = 100 if raw == "OPEN" else 0 if raw == "CLOSE" else device_state["position"]
            else:
                device_state["state"] = raw   # 兜底
            log(f"← [{device_key}] {raw}")
    else:
        # 属性级: room/device/attr/.../set
        # 只支持单级属性（如 speed、oscillation、mode、temp 等）
        attr = attr_segments[0]
        if attr in device_state:
            cur_type = type(device_state[attr]) if device_state[attr] is not None else str
            device_state[attr] = _coerce(raw, cur_type)
            log(f"← [{device_key}] {attr} = {raw}")
        else:
            log(f"← [{device_key}] 未知属性: {attr}")

    _publish_device(c, device_key)

def publish_all(c):
    log("发布全量初始状态")
    for key in state:
        _publish_device(c, key)

def on_connect(c, u, f, rc, p):
    log(f"已连接 Mosquitto ({BROKER}:{PORT})")
    log(f"订阅主题: +/+/set, +/+/+/set, +/+/+/+/set")
    c.subscribe("+/+/set")
    c.subscribe("+/+/+/set")
    c.subscribe("+/+/+/+/set")
    log("发布全量初始状态")
    publish_all(c)

def on_message(c, u, m):
    try:
        raw = m.payload.decode()
        log(f"← {m.topic}: {raw}")  # 立即打印收到的消息
        handle_set(c, m.topic, raw)
    except Exception as e:
        log(f"命令处理出错 [{m.topic}]: {e}")
        import traceback
        traceback.print_exc()

def main():
    acquire_lock()
    log(f"启动 Aether 设备模拟器 (PID={os.getpid()})")
    client_id = f"aether_sim_{os.getpid()}"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    def cleanup(*_):
        log("退出")
        release_lock()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    try:
        client.connect(BROKER, PORT, 60)
        client.loop_forever()
    except ConnectionRefusedError:
        log(f"错误：无法连接 Mosquitto ({BROKER}:{PORT})")
        release_lock()
        sys.exit(1)

if __name__ == "__main__":
    main()
