# MQTT 设备接入协议

Aether 使用 MQTT 协议连接 Home Assistant 和虚拟设备模拟器。本文档定义完整的 Topic 规范、设备模板和接入流程。

---

## 1. 架构

```
┌──────────────┐  MQTT Publish   ┌──────────────┐  REST API   ┌─────────────┐
│ ha_simulator │ ──────────────→ │  Mosquitto    │ ──────────→ │    Home     │
│ (Python)     │ ←────────────── │  :1884        │ ←────────── │  Assistant  │
│              │  MQTT Subscribe  │  (需账号密码)  │  State Sync │   :8123    │
└──────────────┘                  └──────────────┘              └──────┬──────┘
                                                                      │
                                                                 REST API
                                                                      │
                                                              ┌───────▼──────┐
                                                              │    Aether    │
                                                              │   :8010     │
                                                              └──────────────┘
```

- **Mosquitto** (`eclipse-mosquitto:2`，容器名 `mosquitto`）：MQTT Broker，端口 1884，**关闭匿名访问**，需账号密码认证
- **Home Assistant**（容器名 `aether-ha`，本地镜像 `aether-ha:local`）：通过内置 MQTT Integration 订阅设备状态、发送控制指令
- **ha_simulator**：模拟虚拟设备状态并响应控制命令，用 `aether/aether` 账号连接 Broker
- **Aether**：通过 HA REST API（`/api/states`、`/api/services`）读写设备，**不直接连 MQTT**

> docker-compose 仅 **2 个服务**（mqtt + homeassistant）。Aether 主服务在宿主机直接运行（端口 8010），不入 docker-compose。

---

## 2. MQTT Broker 配置

`mosquitto/config/mosquitto.conf`:

```conf
listener 1884
allow_anonymous false
password_file /mosquitto/config/passwd
log_type all
connection_messages true
```

- `allow_anonymous false`：**不允许匿名访问**，所有客户端必须提供账号密码。
- `password_file /mosquitto/config/passwd`：账号文件，挂载自宿主机 `mosquitto/config/passwd`。默认账号 `aether` / 密码 `aether`（用 `mosquitto_passwd` 工具生成，文件内是哈希值）。

如需修改账号，编辑 `passwd` 文件后重启 mosquitto：

```bash
mosquitto_passwd -b mosquitto/config/passwd aether 新密码
docker restart mosquitto
```

---

## 3. Topic 层级规范

### 3.1 通用模式

```
{room}/{device}              状态上报 (JSON 或 简单值)
{room}/{device}/set          控制指令
{room}/{device}/{attribute}  属性状态
{room}/{device}/{attribute}/set  属性控制
```

### 3.2 订阅模式

模拟器订阅以下 MQTT Topic 模式接收 HA 下发的控制指令：

```
+/+/set          # 二维控制: room/device/set
+/+/+/set        # 三维控制: room/device/attr/set
+/+/+/+/set      # 四维控制: room/device/sub/attr/set
```

---

## 4. 设备类型模板

### 4.1 Light (灯)

**HA MQTT YAML 配置:**

```yaml
# ha_config/mqtt/lights.yaml
- name: "设备名称"                    # 显示名称
  unique_id: xxx                     # 全局唯一 ID
  schema: json                       # 使用 JSON 载荷模式
  command_topic: "room/light/set"    # 接收控制指令
  state_topic: "room/light"          # 上报状态
  brightness: true                   # 支持亮度调节
```

**JSON 状态格式:**

```json
{
  "state": "ON",
  "brightness": 128
}
```

`brightness`: 0-255 或 `null` (灯关时)。

**控制指令:**

```json
// 开关
{"state": "ON"}
{"state": "OFF"}

// 调亮度
{"state": "ON", "brightness": 200}
```

**现有设备 Topic:**

| 设备 | 状态 Topic | 控制 Topic | 亮度 |
|------|-----------|-----------|:---:|
| 床头灯 | `bedroom/light` | `bedroom/light/set` | ✓ |
| 厨房灯 | `kitchen/light` | `kitchen/light/set` | ✓ |
| 客厅吊灯 | `living_room/ceiling` | `living_room/ceiling/set` | ✓ |

---

### 4.2 Climate (空调)

**HA MQTT YAML 配置:**

```yaml
# ha_config/mqtt/climates.yaml
- name: "设备名称"
  unique_id: xxx
  mode_command_topic: "room/ac/mode/set"
  mode_state_topic: "room/ac/mode"
  temperature_command_topic: "room/ac/temp/set"
  temperature_state_topic: "room/ac/temp"
  current_temperature_topic: "room/ac/current_temp"
  fan_mode_command_topic: "room/ac/fan/set"
  fan_mode_state_topic: "room/ac/fan"
  modes:
    - "off"
    - "heat"
    - "cool"
    - "auto"
    - "dry"
    - "fan_only"
  fan_modes:
    - "auto"
    - "low"
    - "medium"
    - "high"
  min_temp: 16
  max_temp: 30
  temp_step: 1
  initial: 24
  action_topic: "room/ac/action"
```

**Topic 清单:**

| Topic | 方向 | 载荷类型 | 说明 |
|-------|------|----------|------|
| `living_room/ac/mode` | 设备→HA | string | 模式: `off`/`heat`/`cool`/`auto`/`dry`/`fan_only` |
| `living_room/ac/mode/set` | HA→设备 | string | 模式控制 |
| `living_room/ac/temp` | 设备→HA | string | 设定温度 (如 `24`) |
| `living_room/ac/temp/set` | HA→设备 | string | 温度控制 |
| `living_room/ac/current_temp` | 设备→HA | string | 当前室温 (如 `26.0`) |
| `living_room/ac/fan` | 设备→HA | string | 风速: `auto`/`low`/`medium`/`high` |
| `living_room/ac/fan/set` | HA→设备 | string | 风速控制 |
| `living_room/ac/action` | 设备→HA | string | 当前动作 |

**现有设备:**

| 设备 | unique_id | 房间 |
|------|-----------|------|
| 中央空调 | `hvac` | 客厅 |

---

### 4.3 Cover (窗帘/卷帘)

**HA MQTT YAML 配置:**

```yaml
# ha_config/mqtt/covers.yaml
- name: "设备名称"
  unique_id: xxx
  device_class: curtain
  command_topic: "room/curtain/set"
  state_topic: "room/curtain"
  payload_open: "OPEN"
  payload_close: "CLOSE"
  payload_stop: "STOP"
  position_topic: "room/curtain/position"
  set_position_topic: "room/curtain/position/set"
```

**Topic 清单:**

| Topic | 方向 | 载荷 | 说明 |
|-------|------|------|------|
| `living_room/curtain` | 设备→HA | `open`/`closed`/`opening`/`closing` | 状态 |
| `living_room/curtain/set` | HA→设备 | `OPEN`/`CLOSE`/`STOP` | 控制 |
| `living_room/curtain/position` | 设备→HA | `0`-`100` | 位置百分比 |
| `living_room/curtain/position/set` | HA→设备 | `50` | 位置控制 |

**现有设备:**

| 设备 | unique_id | 房间 |
|------|-----------|------|
| 客厅窗帘 | `living_room_window` | 客厅 |

---

### 4.4 Fan (风扇)

**HA MQTT YAML 配置:**

```yaml
# ha_config/mqtt/fans.yaml
- name: "设备名称"
  unique_id: xxx
  command_topic: "room/fan/set"
  state_topic: "room/fan"
  state_value_template: "{{ value_json.state }}"
  preset_mode_command_topic: "room/fan/speed/set"
  preset_mode_state_topic: "room/fan/speed"
  oscillation_command_topic: "room/fan/oscillation/set"
  oscillation_state_topic: "room/fan/oscillation"
  preset_modes:
    - "off"
    - "low"
    - "medium"
    - "high"
  payload_on: "ON"
  payload_off: "OFF"
  payload_oscillation_on: "ON"
  payload_oscillation_off: "OFF"
```

**JSON 状态格式:**

```json
{
  "state": "ON",
  "speed": "medium",
  "oscillation": false
}
```

**Topic 清单:**

| Topic | 方向 | 载荷 | 说明 |
|-------|------|------|------|
| `living_room/fan` | 设备→HA | `{"state":"ON","speed":"medium","oscillation":false}` | 完整状态 |
| `living_room/fan/set` | HA→设备 | `{"state":"ON","speed":"high"}` | 控制指令 |
| `living_room/fan/speed` | 设备→HA | `medium` | 风速状态 |
| `living_room/fan/speed/set` | HA→设备 | `high` | 风速控制 |
| `living_room/fan/oscillation` | 设备→HA | `ON`/`OFF` | 摇头状态 |
| `living_room/fan/oscillation/set` | HA→设备 | `ON`/`OFF` | 摇头控制 |

> **注意**: HA 2024+ 使用 `preset_mode_*` 而非 `speed_*`。旧字段已废弃。

**现有设备:**

| 设备 | unique_id | 房间 |
|------|-----------|------|
| 客厅风扇 | `living_room_fan` | 客厅 |

---

## 5. HA configuration.yaml 集成

`ha_config/configuration.yaml`:

```yaml
mqtt:
  light: !include mqtt/lights.yaml
  climate: !include mqtt/climates.yaml
  cover: !include mqtt/covers.yaml
  fan: !include mqtt/fans.yaml
```

---

## 6. 模拟器架构

`ha_config/ha_simulator.py` 实现以下逻辑:

### 6.1 认证与连接

```python
# MQTT 认证凭据（mosquitto 已关闭匿名连接）。
# 支持环境变量覆盖；默认与 mosquitto/config/passwd 中的 aether 用户一致。
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "aether")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "aether")

BROKER = args.host or ("mqtt" if args.docker else "localhost")
PORT = args.port or 1884
```

模拟器默认用 `aether/aether` 连接，可用 `MQTT_USERNAME`/`MQTT_PASSWORD` 环境变量覆盖。

### 6.2 状态管理

```python
state = {
    "bedroom/light":      {"state": "OFF", "brightness": None},
    "kitchen/light":      {"state": "OFF", "brightness": None},
    "living_room/ceiling":{"state": "OFF", "brightness": None},
    "living_room/ac":     {"mode":"cool", "temp":24, "current_temp":26.0,
                           "fan":"auto", "swing":"off"},
    "living_room/curtain":{"position": 100},
    "living_room/fan":    {"state": "OFF", "speed": "low", "oscillation": False},
}
```

### 6.3 工作流程

```
1. 连接 MQTT Broker (localhost:1884, 账号 aether/aether)
2. 订阅控制 Topic: +/+/set, +/+/+/set, +/+/+/+/set
3. 发布全量初始状态 (每个设备的所有 Topic)
4. 进入消息循环:
   a. 接收 MQTT 消息
   b. 解析 Topic → 匹配设备 → 更新内存状态
   c. 发布更新后的状态
```

### 6.4 启动命令

```bash
# 宿主机（连 localhost:1884）
python ha_config/ha_simulator.py

# Docker 内（连 mqtt:1883，需 --docker 切 host）
python ha_config/ha_simulator.py --docker

# 自定义
python ha_config/ha_simulator.py --host X --port Y

# 覆盖账号密码
MQTT_USERNAME=aether MQTT_PASSWORD=新密码 python ha_config/ha_simulator.py
```

> 模拟器状态是**一次性**的。HA 重启后 MQTT 实体变为 `unknown`，必须重启模拟器重新发布初始状态。

---

## 7. 接入新设备

### 步骤

1. **创建 MQTT 配置** → `ha_config/mqtt/{type}.yaml`
2. **注册到 HA** → `ha_config/configuration.yaml` 添加 `!include`
3. **修改模拟器** → 添加初始状态、发布函数、命令处理分支
4. **重启服务**:
   ```bash
   docker compose restart homeassistant
   # 等 HA 完全启动
   python ha_config/ha_simulator.py
   ```
5. **验证**: HA 面板 → 设置 → 设备与服务 → 实体，确认状态非 `unknown`

---

## 8. 测试与调试

### MQTT 手动测试

> mosquitto 已关闭匿名访问，所有命令需带 `-u aether -P aether`。

```bash
# 订阅所有风扇主题
mosquitto_sub -h localhost -p 1884 -u aether -P aether -t "living_room/fan/#" -v

# 打开风扇
mosquitto_pub -h localhost -p 1884 -u aether -P aether -t "living_room/fan/set" -m '{"state":"ON"}'

# 调速
mosquitto_pub -h localhost -p 1884 -u aether -P aether -t "living_room/fan/speed/set" -m "high"

# 开灯
mosquitto_pub -h localhost -p 1884 -u aether -P aether -t "bedroom/light/set" -m '{"state":"ON","brightness":200}'

# 空调设温度
mosquitto_pub -h localhost -p 1884 -u aether -P aether -t "living_room/ac/temp/set" -m "26"

# 窗帘定位
mosquitto_pub -h localhost -p 1884 -u aether -P aether -t "living_room/curtain/position/set" -m "50"
```

### Docker 日志

```bash
# Broker 日志
docker logs mosquitto --tail 50 -f

# HA 日志（容器名 aether-ha，镜像 aether-ha:local）
docker logs aether-ha --tail 30

# 模拟器日志（由启动脚本重定向到文件，非后端写入）
# Windows PowerShell:
Get-Content logs\ha_simulator.log -Tail 20 -Wait
```

> `logs/ha_simulator.log` 是启动脚本（`run_demo_fixed.bat`）的输出重定向，**不是后端写的日志**。后端日志只有 `logs/app.log`（RotatingFileHandler 10MB×5）。详见《08-运维排查/日志查看与问题排查》。

### 常见问题

| 症状 | 原因 | 解决 |
|------|------|------|
| HA 中设备 `unknown` | 模拟器未发布初始状态 | 重启模拟器 |
| 模拟器连接被拒 | mosquitto 账号密码不匹配 | 确认 `passwd` 文件与 `MQTT_USERNAME`/`MQTT_PASSWORD` 一致 |
| `Connection refused: not authorised` | 客户端未带账号或账号错误 | 命令加 `-u aether -P aether`，或检查 `passwd` 文件 |
| `extra keys not allowed` | YAML 使用废弃字段 | 改用 `preset_mode_*` 替代 `speed_*` |
| 控制无响应 | Topic 层级不匹配 | 确认订阅模式 `+/+/set` 覆盖三维/四维 |
| Aether 看不到设备 | HA 中设备状态 `unknown` | 先确保 HA 中设备状态正常，等待最多 5 秒刷新 |

---

## 9. HA REST API 对接

Aether **不直接连 MQTT**，而是通过 HA REST API 读写设备。HA 自身的 MQTT Integration 负责把 MQTT 设备状态暴露为 HA 实体。

| 接口 | 用途 | 缓存 |
|------|------|:---:|
| `GET /api/states` | 获取所有实体状态 | 5 秒 |
| `POST /api/services/{domain}/{service}` | 控制设备 | 无 |
| `GET /api/services` | 获取所有服务定义 | 60 秒 (后台) |
| WebSocket | 获取区域/设备归属 | 60 秒 |

对应 Aether 后端路由：`GET /api/ha/entities`、`GET /api/ha/services`、`POST /api/ha/call_service`（详见《API接口参考》第 4 节）。设备可控项（枚举/滑杆/动作）由 `resolve_controls` 从 HA 实体属性 + 服务定义**动态派生**，零硬编码（详见《03-设备控制/控件类型》）。
