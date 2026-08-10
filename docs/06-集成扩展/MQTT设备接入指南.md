# MQTT 设备接入指南

本文档说明如何通过 MQTT 协议向 Aether 智能家居系统接入新设备。

## 架构概览

```
┌─────────────┐    MQTT     ┌────────────┐   REST API   ┌──────────┐
│  设备模拟器  │ ──────────→ │  Mosquitto  │ ──────────→ │  Home    │
│ (Simulator)  │ ←────────── │  (Broker)   │ ←────────── │ Assistant│
└─────────────┘   发布/订阅  └────────────┘  状态同步    └──────────┘
                                                               │
                                                               │ REST API
                                                               ↓
                                                          ┌──────────┐
                                                          │  Aether  │
                                                          │  Backend │
                                                          └──────────┘
```

- **Mosquitto**：MQTT 消息代理，容器名 `mosquitto`，端口 `1884`
- **Home Assistant**：智能家居平台，容器名 `aether-ha`，官方镜像 `homeassistant/home-assistant:stable`，端口 `8123`，通过 MQTT 集成感知设备
- **Aether**：AI 助手后端，通过 HA REST API 读写设备状态并执行自动化

> docker-compose 有四个服务：`mqtt`（mosquitto）、`homeassistant`（HA 官方镜像）、`aether`（本地构建）、`simulator`（虚拟设备模拟器）。HA 配置通过 `./ha_config` 挂载，只用 `default_config` + 内置 MQTT 集成，无需手动构建镜像。详见《Docker服务部署指南》。

## 前置准备

启动 Docker 服务：

```bash
docker-compose up -d
```

验证服务正常：

```bash
docker ps | grep -E "mosquitto|aether-ha"
```

---

## 接入新设备（以风扇为例）

下面以接入一台「客厅风扇」为例，逐步说明完整流程。（注：风扇已在 demo 模拟器里，这里以它演示完整接入步骤，换成你自己的设备同理。）

### 第一步：理解 MQTT Topic 约定

每个设备通过一组标准化的 MQTT Topic 与 HA 通信：

| Topic 模式 | 方向 | 说明 |
|---|---|---|
| `{room}/{device}` | 设备 → HA | 状态上报（JSON） |
| `{room}/{device}/set` | HA → 设备 | 控制指令 |
| `{room}/{device}/{attr}` | 设备 → HA | 属性状态 |
| `{room}/{device}/{attr}/set` | HA → 设备 | 属性控制 |

本例风扇使用以下 Topic：

| Topic | 载荷示例 | 说明 |
|---|---|---|
| `living_room/fan` | `{"state":"OFF","speed":"low","oscillation":false}` | 风扇完整状态 |
| `living_room/fan/set` | `{"state":"ON","speed":"high"}` | 开关/调速指令 |
| `living_room/fan/speed` | `medium` | 当前风速状态 |
| `living_room/fan/speed/set` | `high` | 调速指令 |
| `living_room/fan/oscillation` | `ON` | 摇头状态 |
| `living_room/fan/oscillation/set` | `OFF` | 摇头控制 |

### 第二步：编写 HA MQTT 配置

在 `ha_config/mqtt/fans.yaml` 中定义设备：

```yaml
- name: "客厅风扇"                            # 显示名称
  unique_id: living_room_fan                  # 唯一 ID（不可重复）
  command_topic: "living_room/fan/set"         # 接收开关指令
  state_topic: "living_room/fan"               # 状态上报
  state_value_template: "{{ value_json.state }}"  # 从 JSON 提取 state
  preset_mode_command_topic: "living_room/fan/speed/set"
  preset_mode_state_topic: "living_room/fan/speed"
  oscillation_command_topic: "living_room/fan/oscillation/set"
  oscillation_state_topic: "living_room/fan/oscillation"
  preset_modes:                                # 可选风速档位
    - "off"
    - "low"
    - "medium"
    - "high"
  payload_on: "ON"
  payload_off: "OFF"
  payload_oscillation_on: "ON"
  payload_oscillation_off: "OFF"
```

### 第三步：注册到 configuration.yaml

在 `ha_config/configuration.yaml` 中引用新配置：

```yaml
mqtt:
  light: !include mqtt/lights.yaml
  climate: !include mqtt/climates.yaml
  cover: !include mqtt/covers.yaml
  fan: !include mqtt/fans.yaml       # ← 新增这一行
```

### 第四步：在模拟器中添加设备逻辑

模拟器的状态发布和命令处理是**通用自动路由**，不需要为单个设备写分支：

- **发布**：`publish_all()` 遍历 `state` 字典，每个设备调 `_publish_device()`——按状态结构自动推断发布格式（light 发 JSON 全量、climate 发多 Topic、fan 发 JSON + 子属性 Topic、cover 发位置 + open/closed 等）。
- **命令**：`handle_set()` 用 `_resolve_device()` 对 topic 做**最长前缀匹配**找到设备，再按目标属性当前类型自动转换值（`_coerce()`）——设备级 `room/device/set` 支持 JSON 或 `ON/OFF/OPEN/CLOSE/STOP` 文本，属性级 `room/device/attr/set` 单级属性直接赋值。

所以接入新设备只需要两步：

**A. 添加初始状态**

```python
state = {
    # ... 已有设备 ...
    "living_room/fan": {"state": "OFF", "speed": "low", "oscillation": False},
}
```

**B. 无需改其它代码**

发布和命令处理会自动覆盖新设备。如果新设备的属性结构比较特殊（比如布尔值想发 `ON/OFF`、数值想拆独立 Topic），在 `_publish_device()` 里加一个分支即可（参考现有 fan / humidifier 分支的写法）。

### 第五步：重启服务验证

```bash
# 方式一：全家桶一起重启（HA + 模拟器，Aether 主服务一般不用动）
docker-compose restart homeassistant simulator

# 方式二：单独重启 HA 和模拟器
docker-compose restart homeassistant
# 等 HA 完全启动后，再重启模拟器
python ha_config/ha_simulator.py
```

### 第六步：确认设备在 Aether 中可见

1. 打开 HA 面板 `http://localhost:8123` → 设置 → 设备与服务 → 实体，搜索设备名，确认状态不是 `unknown`
2. 如果状态为 `unknown`，重新运行模拟器：`python ha_config/ha_simulator.py`
3. 打开 Aether 前端 → 设备列表（`/halist`），确认新设备已出现

> 模拟器发布状态用 `retain=True`（MQTT 保留消息），mosquitto 会保存每个 topic 的最后一条消息——**HA 重启后重连订阅会收到 retained 消息恢复状态**，不需要重跑模拟器。传感器/加湿器这类还会每 60 秒循环重发（顺便从后端拉真实天气做基准）。

## Demo 自带设备 Topic 速查

模拟器 `ha_simulator.py` 默认带 10 个设备：

| 设备 | entity_id 域 | Topic | 初始载荷 |
|---|---|---|---|
| 床头灯 | `light` | `bedroom/light` | `{"state":"OFF","brightness":null}` |
| 厨房灯 | `light` | `kitchen/light` | `{"state":"OFF","brightness":null}` |
| 客厅吊灯 | `light` | `living_room/ceiling` | `{"state":"OFF","brightness":null}` |
| 中央空调 | `climate` | `living_room/ac/mode` | `cool`（temp/current_temp/fan/swing 各一 Topic） |
| 客厅窗帘 | `cover` | `living_room/curtain` | `open`（position=100 单独一个 Topic） |
| 客厅风扇 | `fan` | `living_room/fan` | `{"state":"OFF","speed":"low","oscillation":false}` |
| 温湿度传感器 | `sensor` | `living_room/sensor` | `{"temperature":26.5,"humidity":58}`（每 60s 更新） |
| 智能插座 ×2 | `switch` | `bedroom/plug`、`kitchen/plug` | `{"state":"OFF"}` |
| 加湿器 | `humidifier` | `bedroom/humidifier` | `{"state":"OFF","target_humidity":50,...}` |

控制指令统一走 `.../set` Topic，属性控制走 `.../{attr}/set`。

## 设备类型配置模板

### Light（灯）

```yaml
- name: "设备名"
  unique_id: xxx
  schema: json
  command_topic: "room/device/set"
  state_topic: "room/device"
  brightness: true          # 支持亮度调节
```

模拟器状态格式：`{"state": "ON"|"OFF", "brightness": 0-255 | null}`

### Climate（空调）

```yaml
- name: "设备名"
  unique_id: xxx
  mode_command_topic: "room/ac/mode/set"
  mode_state_topic: "room/ac/mode"
  temperature_command_topic: "room/ac/temp/set"
  temperature_state_topic: "room/ac/temp"
  current_temperature_topic: "room/ac/current_temp"
  modes: ["off","heat","cool","auto","dry","fan_only"]
  min_temp: 16
  max_temp: 30
  temp_step: 1
```

模拟器中每个属性一个 Topic，值为字符串/数字。

### Cover（窗帘/卷帘）

```yaml
- name: "设备名"
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

### Fan（风扇）

```yaml
- name: "设备名"
  unique_id: xxx
  command_topic: "room/fan/set"
  state_topic: "room/fan"
  state_value_template: "{{ value_json.state }}"
  preset_mode_command_topic: "room/fan/speed/set"
  preset_mode_state_topic: "room/fan/speed"
  oscillation_command_topic: "room/fan/oscillation/set"
  oscillation_state_topic: "room/fan/oscillation"
  preset_modes: ["off","low","medium","high"]
  payload_on: "ON"
  payload_off: "OFF"
  payload_oscillation_on: "ON"
  payload_oscillation_off: "OFF"
```

模拟器状态格式：`{"state":"OFF","speed":"low","oscillation":false}`

> **注意**：风扇使用 `preset_mode_*` 而非 `speed_*`，这是 HA 2024+ 版本的 MQTT Fan 标准格式。旧版 `speed_command_topic` 和 `speeds` 已废弃，使用会导致配置校验失败。

## 测试设备

用 `mosquitto_pub` 手动测试 MQTT 通信：

```bash
# 查看风扇状态（broker 关了匿名，要带账号密码）
mosquitto_sub -h localhost -p 1884 -u aether -P aether -t "living_room/fan/#"

# 打开风扇
mosquitto_pub -h localhost -p 1884 -u aether -P aether -t "living_room/fan/set" -m '{"state":"ON"}'

# 调至高速
mosquitto_pub -h localhost -p 1884 -u aether -P aether -t "living_room/fan/speed/set" -m "high"

# 开启摇头（模拟器状态里有 oscillation 字段，HA 侧风扇模板没配 oscillation topic 的话只是模拟器收到，HA 不会反映）
mosquitto_pub -h localhost -p 1884 -u aether -P aether -t "living_room/fan/oscillation/set" -m "ON"
```

## 常见问题

### HA 中看不到新设备

1. 确认 `configuration.yaml` 中已 include 对应 yaml
2. 重启 HA：`docker-compose restart homeassistant`
3. 等待 MQTT 自动发现（最多 30 秒）
4. 检查 HA 日志确认配置无错误：`docker logs aether-ha | grep -i "fan\|error"`
5. 如果看到 `extra keys not allowed` 错误，说明 YAML 使用了已废弃的字段名（如 `speed_command_topic` 应改为 `preset_mode_command_topic`），参考上方模板修正

### 控制无响应

1. 确认 MQTT 订阅正确：模拟器订阅 `+/+/set`、`+/+/+/set`、`+/+/+/+/set`
2. 检查日志：`docker logs mosquitto`
3. 确认 Topic 层级匹配：三维 `room/device/set`，四维 `room/device/attr/set`

### Aether 中设备不显示

Aether 通过 HA REST API 获取设备列表，需要满足以下全部条件才能看到设备：

1. **HA 中实体已注册**：访问 `http://localhost:8123/config/entities` 搜索设备名，确认实体存在且 `entity_id` 格式正确（如 `fan.xxx`、`light.xxx`）
2. **模拟器已发布初始状态**：MQTT 设备的状态发布带 `retain=True`（保留消息），HA 重启重连订阅后会自动收到 retained 消息恢复状态，通常不需要重跑模拟器。如果实体仍为 `unknown`（比如 broker 数据被清过），再重启一次模拟器（`python ha_config/ha_simulator.py`）让它重新发布全量状态即可
3. **Aether 后端已同步**：设备在 HA 中状态变为正常后，Aether 会自动刷新设备列表即可看到新设备

> **关键**：设备在 HA 中注册后，状态必须从 `unknown` 变为实际值（如 `ON/OFF`），Aether 才会将其纳入设备列表。如果一直是 `unknown`，Aether 看不到这个设备。

## Aether 怎么用这些设备

Aether 不直接碰 MQTT，它只跟 HA 的 REST API 打交道。设备经 MQTT 接入 HA 后，Aether 这边：

- `/halist`（设备列表页）能看到设备，控件类型从 HA 的属性和服务**动态推导**（开关/按钮组/滑块/动作按钮），不写死。
- AI 管家对话里可以用 `call_service` 控制、`get_entities` 查询。
- 可以把设备状态写进自动化规则的条件或动作里。

设备控件怎么从 HA 属性推导出来，详见《设备控件类型说明》。
