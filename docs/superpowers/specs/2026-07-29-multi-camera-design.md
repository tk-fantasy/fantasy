# 多摄像头改造方案

- **日期**: 2026-07-29
- **状态**: 已批准(待实现)
- **目标**: 把 Aether 现有的**单摄像头单例**架构,改造为**一台 Aether 管理多路(首期 4 路)摄像头**的架构,每路独立配置/采集/运动门控,共享受控的云端 VLM 并发预算,并整合 ONVIF 自动发现(每路独立 MAC 绑定,DHCP 换 IP 各自找回)。
- **关系**: 本方案取代根目录草稿《多摄像头改造方案.md》,并把 `2026-07-29-onvif-camera-discovery-design.md` 的发现能力整合进多路场景。两份旧文档作废,以此为准。

---

## 1. 背景与现状

### 1.1 现状资产

当前摄像头子系统是**全局单例**:

- `app/camera_stream.py` — `CameraStream` 单实例,采集线程 + 运动门控(dHash)+ 推理调度 + MJPEG 预览全部耦合在一个组件。
- 配置来源:`config.json` 的 `vision`(rtsp/运动参数/视觉参数)、`ptz`(云台)、`automation.camera_vl_display_enabled`(视觉展示开关)三个全局段。
- 视觉关注项:存在 KV 表 `db.kv_get("vision_focuses")`,值是一个 JSON 数组(**不是独立表**)。
- 自动化规则:`rules` 独立表,`data` 是 JSON blob,无摄像头归属概念。
- 路由:`/api/video_feed`(单一 MJPEG 端点)、`/api/ptz/*`、`/api/automation/*`、视觉关注项在 `/api/settings/vision/focus*`。
- 摄像头 ID:代码里没有任何 ID 概念,只有一个全局摄像头。

### 1.2 要解决的问题

1. 多路摄像头各自有不同来源(RTSP/USB)、不同参数(运动阈值、区域)、不同 PTZ 配置 → 全局单例撑不住。
2. 四路都无差别跑 VLM → token 费用爆炸 + 云端 429 风险。
3. 多路中含 RTSP 摄像头,DHCP 换 IP 后各自要能找回(ONVIF 发现要支持多路,而非全局单一设备)。
4. 自动化规则、视觉关注项需要绑定到具体摄像头。

### 1.3 关键决策(已与用户确认)

| # | 决策 | 选择 |
|---|------|------|
| 1 | ID 方案 | **稳定随机 ID**(`cam_<6位>`,生成一次永不改)+ 用户可改的 `name` + 显示序号 `sort_order`(删除留空缺不重排) |
| 2 | 流协议 | **MJPEG**,不引 ZLM。未来录像时新增独立 H.264 转码链路,不推翻 MJPEG 预览 |
| 3 | VLM 并发 | 云端 glm-4v(上限 10)。**展示推理全局 1 路(独立通道)**;**自动化 + 工具调用共享并发池,上限 5,超过排队**;峰值 1+5=6 < 10,安全 |
| 4 | 推理分工 | 采集每路独立全速;展示推理只跑 `/camera` 弹窗当前看的那路;有规则的摄像头在运动触发后跑自动化评估,没被看的路不跑展示推理 |
| 5 | ONVIF 发现 | 一并整合。`cameras` 表加 `device_mac`,每路独立 MAC 绑定,换 IP 各自找回 |
| 6 | 数据迁移 | 现有 config.json 的 `vision`/`ptz`/`automation.camera_vl_display_enabled` 迁到 `cameras` 表一条记录(ID 随机,名"默认摄像头")。**迁移后删除 config.json 这三个段**,配置真源唯一 |
| 7 | 规则/关注项绑定 | `rules` 加显式 `camera_id` 列;`vision_focuses` KV JSON 每条加 `camera_id` 字段(保持 KV 存储,不建新表) |
| 8 | 定时任务 | 不绑摄像头(用户说"看下客厅"时由 LLM 从可用摄像头列表推断) |
| 9 | 前端范围 | 后端 + 前端全量做 |

---

## 2. 架构设计

### 2.1 新增组件

```
app/services/
  camera_manager.py          ← 新增:多路摄像头生命周期管理 + VLM 并发调度
  camera_discovery_service.py ← 新增(整合 ONVIF):每路独立 MAC 发现
```

### 2.2 职责划分

| 组件 | 职责 | 不做什么 |
|------|------|---------|
| `CameraManager` | 管 `dict[camera_id → CameraStream]` 生命周期;CRUD 转发到 DB;管理**展示推理通道**(全局 1 路);管理**自动化+工具 VLM 并发池**(上限 5) | 不实现采集/运动门控逻辑(那是 CameraStream 的事);不直接跑 VLM |
| `CameraStream` | 单路采集 + dHash 运动门控 + MJPEG 预览;请求推理时**回调** manager 申请 VLM 名额 | 不自己决定并发上限(向 manager 申请) |
| `CameraDiscoveryService` | 每路独立子网扫描 + MAC 匹配 + config(数据库)回写 + 通知重连 | 不管采集、不管云台动作 |
| `AutomationAgent` | 触发评估(改动:传入 `camera_id`,按摄像头取规则) | 不直接管 VLM 并发(通过 manager) |

### 2.3 VLM 并发调度模型

两个独立通道,各自有上限,避免互相饿死:

```
┌─ 展示推理通道(上限 1)──────────────────────┐
│  /camera 弹窗当前看的那一路持续跑            │
│  切换:旧的 stop → 新的 start               │
│  弹窗关闭:0 路                            │
│  → 用户看画面时不被自动化占满配额而卡顿      │
└────────────────────────────────────────────┘

┌─ 自动化+工具通道(上限 5)────────────────────┐
│  来源:① 运动触发的自动化评估(多路并行)     │
│        ② 聊天工具调用(vision_chat/verify)   │
│  Semaphore(5):超过排队等                   │
│  → 运动稀疏时队列很快消化                   │
└────────────────────────────────────────────┘

峰值 = 1 + 5 = 6 < glm-4v 上限 10 ✓
```

**为什么展示推理独立通道不占 5 的配额**:用户正在看画面时,如果 5 个自动化名额全占满,展示推理要排队 → 画面卡顿。家庭场景峰值 6 远在云端上限内,分开通道让用户体验和自动化都不被对方拖累。

### 2.4 调用关系

```
bootstrap 启动
  └─ CameraManager.initialize()
        ├─ 从 cameras 表加载所有 enabled 摄像头
        ├─ 每路 new CameraStream(camera_id, config_dict, ...)
        ├─ 每路注入 discovery_service(各自独立 MAC)
        └─ 每路 start()

/camera 弹窗打开(camera_id)
  └─ manager.enable_display(camera_id)
        ├─ 若有旧展示路:旧路 stop_display
        └─ 新路 start_display(进入展示通道)

运动触发(某路)
  └─ CameraStream 检测 dHash → 回调 manager.request_automation_eval(camera_id)
        └─ manager: 拿自动化通道 Semaphore(5) 名额 → 跑该路规则评估

用户聊天调 vision_chat(camera_id)
  └─ tool handler → manager.request_tool_inference(camera_id)
        └─ 同走自动化+工具通道(共享 Semaphore(5))

某路 RTSP 掉线
  └─ CameraStream._worker 连续开流失败 → discovery.find_camera(camera_id)
        └─ 用该路 device_mac 在该路子网找回 IP → 更新 DB → worker 重连
```

---

## 3. 数据模型

### 3.1 新增 `cameras` 表

```sql
CREATE TABLE IF NOT EXISTS cameras (
    id              TEXT PRIMARY KEY,          -- cam_<6位随机>,生成一次永不改
    name            TEXT NOT NULL DEFAULT '',  -- 用户可改名
    enabled         INTEGER DEFAULT 1,
    sort_order      INTEGER DEFAULT 0,         -- 显示序号,删除留空缺不重排
    source_type     TEXT NOT NULL DEFAULT 'usb',  -- usb | rtsp
    -- 来源
    usb_index       INTEGER,
    rtsp_url        TEXT DEFAULT '',
    rtsp_username   TEXT DEFAULT '',
    rtsp_password   TEXT DEFAULT '',           -- 明文存(与现有 .env 模式一致,见 §3.3)
    -- 归属区域(LLM 推断 + 展示用)
    area            TEXT DEFAULT '',
    -- ONVIF 自动发现
    device_mac      TEXT DEFAULT '',           -- 该路身份,换 IP 按 MAC 找回
    discovery_enabled INTEGER DEFAULT 1,
    -- PTZ 云台
    ptz_enabled     INTEGER DEFAULT 0,
    ptz_ip          TEXT DEFAULT '',
    ptz_port        INTEGER DEFAULT 80,
    ptz_username    TEXT DEFAULT '',
    ptz_password    TEXT DEFAULT '',
    ptz_speed       REAL DEFAULT 0.5,
    ptz_step_ms     INTEGER DEFAULT 300,
    -- 运动检测(dHash)
    motion_hash_size           INTEGER DEFAULT 16,
    motion_threshold           INTEGER DEFAULT 15,
    motion_check_interval      REAL DEFAULT 1.0,
    -- 视觉推理参数
    vision_downscale            INTEGER DEFAULT 448,
    vision_jpeg_quality         INTEGER DEFAULT 60,
    vision_min_infer_interval   REAL DEFAULT 8.0,
    vision_max_idle_interval    REAL DEFAULT 120.0,
    vision_use_img_count        INTEGER DEFAULT 3,
    frame_interval_ms           INTEGER DEFAULT 1000,
    -- 展示开关(每路独立,替代旧全局 automation.camera_vl_display_enabled)
    display_enabled             INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

> **说明**:每路摄像头的运动/视觉参数都是独立列,而非塞 JSON blob。理由:这些参数要能按摄像头在 SQL 层筛选、排序、单独更新(滑块热更新),JSON blob 做不到。`rules` 表因为是"按规则管理"语义且字段多变动,保留 JSON blob + 加 `camera_id` 显式列。

### 3.2 关联表改造

**`rules` 表加显式列**(便于按摄像头查询规则):

```sql
ALTER TABLE rules ADD COLUMN camera_id TEXT DEFAULT '';
```

通过 `database.py` 的 `_ensure_column` 幂等迁移(与现有 user_id 迁移同模式)。`camera_id` 为空表示未绑定(向后兼容老规则),评估时归到"默认摄像头"或由 LLM 推断。

**`vision_focuses` 保持 KV 存储**(不建新表):

KV 值是一个 JSON 数组,每条加 `camera_id` 字段:

```json
[
  {"id": "focus_1", "text": "人", "enabled": true, "camera_id": "cam_a1b2c3"},
  {"id": "focus_2", "text": "包裹", "enabled": true, "camera_id": "cam_a1b2c3"}
]
```

> **为什么 vision_focuses 不建表**:它是 VisionService 内存持有的列表,KV 存储已够用,且现有代码(`vision_service._vision_focuses` + `db.kv_set`)成熟稳定。建表收益小、改动面大,YAGNI。

**`scheduled_tasks` 表不加 camera_id**(见 §1.3 决策 8)。

### 3.3 密钥存储策略

现状是 config.json 存 `rtsp_password_env`(env 变量名)+ `.env` 存明文。多路后每路有自己的凭证,环境变量模式(每路一个 `RTSP_PASSWORD_cam_xxx`)会膨胀且难管理。

**决策**:`cameras` 表的密码字段存**明文**(`rtsp_password` / `ptz_password`)。

理由:
1. RTSP/PTZ 密码是**家用摄像头凭证**,不是 LLM API key 那种高价值密钥,泄露面 = 局域网内能读 SQLite 的人(已是信任域)。
2. 现有 `ptz.password_env` 走 env,但 PTZ 本就是单设备;多路后每路独立凭证,明文进 DB 最简单,且 CameraStream 构造时直接拿到,不用再做 env 解析。
3. 迁移时把现有 `.env` 里的 `RTSP_PASSWORD` / `PTZ_PASSWORD` 值读出来写进 `cameras` 表第一条记录,然后从 `.env` 删除。

> 安全权衡:DB 文件权限由部署者控制(与现有 user_settings 表里存明文 LLM key 一致,这是项目既有模式)。

---

## 4. 迁移方案

迁移在 `Database.init()` 中执行,幂等(可重复运行)。逻辑:

```
cameras 表不存在 → 建表
  │
  ├─ 检测 config.json 是否有旧 vision/ptz/automation 段
  │     │
  │     ├─ 有(老部署):
  │     │     1. 生成 ID: cam_<6位随机>
  │     │     2. name = "默认摄像头", sort_order = 0
  │     │     3. 从 config.json vision 段读:source_type(有 rtsp_url→rtsp,否则 usb)、
  │     │        rtsp_*、运动参数、视觉参数、frame_interval_ms、display_enabled
  │     │     4. 从 config.json ptz 段读:ptz_* 全套
  │     │     5. 从 .env 读 RTSP_PASSWORD / PTZ_PASSWORD 写入 rtsp_password / ptz_password
  │     │     6. device_mac 留空(首次连接时由 discovery 捕获,或手动发现时读)
  │     │     7. INSERT 进 cameras 表
  │     │     8. 把 rules 表所有现有规则的 camera_id 设为这个新 ID
  │     │     9. 把 vision_focuses KV 每条加 camera_id = 新 ID,写回 KV
  │     │    10. 从 config.json 删除 vision / ptz / automation.camera_vl_display_enabled
  │     │    11. 从 .env 删除 RTSP_PASSWORD / PTZ_PASSWORD
  │     │
  │     └─ 无(全新部署):跳过迁移,cameras 表为空,首次进 UI 添加
  │
  ├─ rules 表无 camera_id 列 → ALTER TABLE ADD COLUMN(幂等)
  └─ 已迁移过的部署:cameras 表已存在且非空 → 跳过
```

**幂等保证**:`cameras` 表存在且非空 = 已迁移,跳过整个迁移块。这样新部署升级、二次重启都不会重复迁移。

**config.json 字段映射**(迁移用):

| config.json 路径 | cameras 表字段 |
|------------------|----------------|
| `vision.rtsp_url` | `rtsp_url`(非空 → `source_type=rtsp`) |
| `vision.rtsp_username` | `rtsp_username` |
| env `RTSP_PASSWORD` | `rtsp_password` |
| `vision.motion_hash_size` | `motion_hash_size` |
| `vision.motion_threshold` | `motion_threshold` |
| `vision.motion_check_interval_seconds` | `motion_check_interval` |
| `vision.downscale_max_side` | `vision_downscale` |
| `vision.jpeg_quality` | `vision_jpeg_quality` |
| `vision.min_infer_interval_seconds` | `vision_min_infer_interval` |
| `vision.max_idle_interval_seconds` | `vision_max_idle_interval` |
| `vision.vision_use_img_count` | `vision_use_img_count` |
| `vision.frame_interval_ms` | `frame_interval_ms` |
| `automation.camera_vl_display_enabled` | `display_enabled` |
| `ptz.enabled` | `ptz_enabled` |
| `ptz.ip` | `ptz_ip` |
| `ptz.port` | `ptz_port` |
| `ptz.username` | `ptz_username` |
| env `PTZ_PASSWORD` | `ptz_password` |
| `ptz.speed` | `ptz_speed` |
| `ptz.step_ms` | `ptz_step_ms` |

---

## 5. ONVIF 发现整合(多路化)

基于 `2026-07-29-onvif-camera-discovery-design.md`(单设备版),改为**按 camera_id 参数化**:

### 5.1 CameraDiscoveryService 改造

```python
class CameraDiscoveryService:
    async def read_device_hardware_id(self, ip, port, user, pwd) -> str
    async def find_camera(self, camera_id: str) -> str | None
        # 读该路 cameras.device_mac + ptz_ip 推子网
        # 子网两段式扫描 → MAC 匹配 → 返回新 IP
    async def apply_found_ip(self, camera_id: str, new_ip: str) -> None
        # 更新 cameras.rtsp_url(host)+ ptz_ip(双写)
        # 通知该路 CameraStream + PtzService 重连
    async def capture_mac_on_startup(self, camera_id: str) -> None
    async def find_and_apply(self, camera_id: str) -> str | None
```

**与单设备版的核心区别**:所有方法多一个 `camera_id` 参数;MAC、子网、凭证从 `cameras` 表对应行读,而非全局 config。

### 5.2 每路独立触发

```
CameraStream._worker(camera_id) 连续开流失败
  └─ discovery_enabled(读 cameras 表该路)
        └─ discovery.find_camera(camera_id)
              命中 → apply_found_ip(camera_id, new_ip)
                     → 更新 DB → 通知该路 worker 重连 + ptz 重连
```

每路的发现互相独立,一台掉线不影响其他路。

### 5.3 首次 MAC 捕获

启动时对所有 `discovery_enabled` 且 `device_mac` 为空的路,后台并发捕获 MAC(复用 §2.3 的并发控制,但发现阶段不走 VLM 通道,独立并发,可放宽):

```python
# bootstrap 启动后,后台任务
for cam in cameras where discovery_enabled and device_mac == "":
    asyncio.create_task(discovery.capture_mac_on_startup(cam.id))
```

---

## 6. 后端改造

### 6.1 CameraManager(新增)

```python
class CameraManager:
    def __init__(self, vision_service, motion_service, ha_service, db):
        self._streams: dict[str, CameraStream] = {}
        self._active_display_id: str | None = None  # /camera 弹窗当前看的
        self._display_sem = asyncio.Semaphore(1)    # 展示推理通道(上限 1)
        self._auto_sem = asyncio.Semaphore(5)       # 自动化+工具通道(上限 5)

    async def initialize(self):
        """启动时从 DB 加载所有 enabled 摄像头。"""
    async def create_camera(self, data: dict) -> dict
    async def update_camera(self, camera_id: str, data: dict) -> dict
    async def delete_camera(self, camera_id: str) -> bool

    async def enable_display(self, camera_id: str)   # 弹窗打开/切换
    async def disable_display(self, camera_id: str)  # 弹窗关闭

    async def get_frame(self, camera_id: str) -> bytes
    async def get_recent_frames(self, camera_id: str, n: int) -> list
    def mjpeg_generator(self, camera_id: str)        # /video_feed 用
    def get_state(self, camera_id: str) -> dict

    async def request_automation_eval(self, camera_id: str, frames):
        """运动触发:拿 _auto_sem 名额 → 跑该路规则评估。"""
    async def request_tool_inference(self, camera_id: str, prompt, frames):
        """工具调用:共享 _auto_sem → vision_client 调用。"""

    def list_cameras(self) -> list[dict]  # 供 LLM 工具注入(§6.4)
```

### 6.2 CameraStream 改造

构造函数参数化,不再读全局 config:

```python
class CameraStream:
    def __init__(self, camera_id: str, config: dict,
                 vision_service, motion_service,
                 on_automation_trigger=None,  # 回调 manager
                 discovery_service=None):
        self.camera_id = camera_id
        self._config = config  # 单路配置 dict(从 cameras 表行映射)
        # ... 所有参数从 self._config 读
```

**关键改动**:
- 去掉 `set_camera_vl_display_enabled` 全局开关,改为 `start_display()` / `stop_display()` 方法,由 manager 调用。
- 运动触发时不再自己决定是否推理,而是**回调** `on_automation_trigger(camera_id)` 让 manager 决策(走 §2.3 并发模型)。
- `set_discovery_service(discovery_service)` 注入,worker 掉线时带 `camera_id` 触发发现。

### 6.3 路由设计(新增 `camera_routes.py`)

合并现有散落的 PTZ / 视觉关注项 / 规则端点:

```
# 摄像头管理
GET    /api/cameras                         # 列表(含在线状态)
POST   /api/cameras                         # 新增
GET    /api/cameras/{camera_id}             # 详情
PUT    /api/cameras/{camera_id}             # 更新
DELETE /api/cameras/{camera_id}             # 删除

# 视频流
GET    /api/cameras/{camera_id}/video_feed  # MJPEG
POST   /api/cameras/{camera_id}/test-stream # 测试连接

# 展示推理控制
POST   /api/cameras/{camera_id}/display/enable
POST   /api/cameras/{camera_id}/display/disable

# 运行状态
GET    /api/cameras/{camera_id}/state

# PTZ 云台(从 ptz_routes.py 迁入)
POST   /api/cameras/{camera_id}/ptz/move
POST   /api/cameras/{camera_id}/ptz/stop
POST   /api/cameras/{camera_id}/ptz/step
GET    /api/cameras/{camera_id}/ptz/status
POST   /api/cameras/{camera_id}/ptz/test

# 视觉关注项(从 settings_routes.py 迁入)
GET    /api/cameras/{camera_id}/focuses
POST   /api/cameras/{camera_id}/focuses
PUT    /api/cameras/{camera_id}/focuses/{focus_id}
DELETE /api/cameras/{camera_id}/focuses/{focus_id}

# ONVIF 发现(从 discovery_routes.py,见 §6.5)
POST   /api/cameras/{camera_id}/discovery/find
GET    /api/cameras/{camera_id}/discovery/status
POST   /api/cameras/{camera_id}/discovery/manual-ip
```

> **规则端点保留在 `rule_routes.py`**,不迁入 camera_routes(规则是跨摄像头管理的实体,虽带 camera_id 但不嵌套)。规则的创建/列表支持按 `camera_id` 查询参数过滤。

### 6.4 vision_chat / verify_condition 工具适配

`app/tools.py` 工具描述中注入可用摄像头列表,LLM 按区域/名称推断:

```
可用摄像头:
  - cam_a1b2c3 (客厅摄像头, 区域:客厅, 在线)
  - cam_d4e5f6 (卧室摄像头, 区域:卧室, 在线)
  - cam_g7h8i9 (门口摄像头, 区域:玄关, 离线)
```

工具参数加可选 `camera_id`:
- 用户指定 → 用指定的
- 未指定 → 优先取 `/camera` 弹窗当前看的(`manager._active_display_id`)
- 都没有 → LLM 根据自然语言(区域/名称)推断

### 6.5 容器与启动改造

- `AppContainer`:`camera_stream` 字段 → `camera_manager`(CameraManager)。
- `bootstrap.py`:`CameraManager` 替代 `CameraStream`,注入 discovery_service。
- `main.py`:lifespan 改为 `camera_manager.initialize()`,后台 MAC 捕获遍历所有路。
- `AutomationAgent`:触发评估时带 `camera_id`,`AutomationService.evaluate(frames, camera_id)` 按摄像头取规则。

---

## 7. 前端改造

### 7.1 新增页面:CameraSettingsView

路由 `/cameras`,侧栏"摄像头管理"。卡片列表 + 编辑面板(详见草稿已设计,本方案沿用):
- 基本信息:名称、区域(HA `/api/areas` 下拉)、来源(USB/RTSP)、来源字段、测试连接
- PTZ 云台:启用 ONVIF、IP/端口/凭证/速度/步进、测试
- ONVIF 发现:启用开关、device_mac(只读,自动捕获)、手动发现按钮、手动填 IP
- 高级参数:运动检测、视觉推理参数(折叠)
- 视觉关注项:该路的 focus 列表
- 自动化规则:该路的规则(跳转或内嵌,用与 ChatView 一致的 LLM 解析)

### 7.2 /camera 弹窗改造

`ChatView.vue` 顶部加摄像头切换标签:

```
[客厅 ▾  卧室  门口]           [×]   ← 切换 activeCameraId
        <img :src="video_feed(activeCameraId)" />
        ▲ PTZ(当前路)
        ☑ 视觉展示推理(当前路的 display_enabled)
        运动距离│累计推理│模型FPS(当前路)
```

**VLM 调用控制**:
- 弹窗打开 → `POST display/enable(当前)` → manager 切换展示通道
- 切换摄像头 → `POST display/disable(旧)` → `POST display/enable(新)`
- 弹窗关闭 → `POST display/disable`

### 7.3 前端文件变化

```
新增:
  frontend/src/views/CameraSettingsView.vue
  frontend/src/composables/useCamera.js
修改:
  frontend/src/views/ChatView.vue          # /camera 弹窗加切换
  frontend/src/views/MonitorView.vue       # 摄像头监控适配多路
  frontend/src/views/FocusView.vue         # → 合并到 CameraSettingsView(或保留按摄像头过滤)
  frontend/src/router/index.js             # 新路由 /cameras
  frontend/src/components/SidebarNav.vue   # 新导航项
  frontend/src/utils/api.js                # cameraAPI
删除:
  app/routes/ptz_routes.py                 # 合并到 camera_routes
```

---

## 8. 配置清理

迁移完成后,从 `config.json` 和 `config.example.json` 删除:
- `vision` 段(全部字段 → cameras 表)
- `ptz` 段(全部字段 → cameras 表)
- `automation.camera_vl_display_enabled`(→ cameras.display_enabled)

`automation` 段保留其余字段(`eval_interval_seconds`、`silent_eval_*`、`default_cooldown_seconds` 仍是全局自动化参数,与摄像头无关)。

`app/core/config.py` 中 `vision.*` / `ptz.*` 的属性访问同步删除,改为从 `cameras` 表读。

---

## 9. 错误处理与边界

| 情况 | 行为 |
|------|------|
| 某路摄像头启动失败(USB 占用/RTSP 不通) | 不影响其他路;该路状态标 offline,UI 显示离线;worker 指数退避重试 |
| RTSP 路掉线 + discovery_enabled | 该路独立触发 ONVIF 发现,找回后重连;其他路不受影响 |
| discovery 超时未命中 | 该路状态「找不到设备」,UI 开放手动填 IP |
| 四路同时运动触发自动化 | Semaphore(5) 排队,第 5 路后等;队列消化后逐个跑 |
| 用户看画面时自动化占满 5 名额 | 展示推理独立通道,不受影响,画面不卡 |
| 删除摄像头时有关联规则/关注项 | 规则 `camera_id` 置空(保留规则但解绑);关注项一并删除 |
| 迁移后旧版本回滚 | config 段已删,DB 有数据;回滚需手动重建 config 或重新配置(代价已知,接受) |
| device_mac 为空(USB 摄像头) | 该路 discovery 自动跳过(USB 无 DHCP 换 IP 问题) |

---

## 10. 范围与非目标

**本期做**:
- 多路摄像头管理(cameras 表 + CameraManager + CameraStream 参数化)
- VLM 双通道并发调度(展示 1 + 自动化/工具 5)
- ONVIF 发现多路化(每路独立 MAC 绑定 + 换 IP 各自找回)
- 数据迁移(config.json → cameras 表 + 删除旧段)
- 规则/关注项绑定 camera_id
- 前端全量(摄像头管理页 + /camera 弹窗切换 + MonitorView 适配)

**本期不做(YAGNI)**:
- 录像 / 回放(未来需要时新增 ZLM H.264 转码链路,不推翻 MJPEG 预览)
- 四宫格同屏(本期切换看一次一路;同屏需求出现时再加,降帧降分辨率即可)
- 分布式多节点(多台 Aether 服务器协同)——超出单家庭场景
- 摄像头自动发现(扫描局域网自动列出可添加摄像头)——仅做"已配摄像头换 IP 找回"

---

## 11. 文件变化汇总

```
新增:
  app/services/camera_manager.py
  app/services/camera_discovery_service.py
  app/routes/camera_routes.py
  tests/test_camera_manager.py
  tests/test_camera_discovery_service.py
  tests/test_camera_routes.py
  frontend/src/views/CameraSettingsView.vue
  frontend/src/composables/useCamera.js

修改:
  app/core/database.py               # cameras 表 + 迁移 + camera_id 列
  app/camera_stream.py               # 参数化重构 + start/stop_display + 回调 manager
  app/services/ptz_service.py        # 参数化(按 camera_id)+ notify_ip_changed
  app/services/vision_service.py     # 方法加 camera_id(关注项按摄像头)
  app/services/automation_service.py # evaluate(frames, camera_id) 按摄像头取规则
  app/services/rule_service.py       # CRUD 支持 camera_id
  app/agents/automation_agent.py     # 触发带 camera_id
  app/bootstrap.py                   # CameraManager 替代 CameraStream + discovery 注入
  app/container.py                   # camera_manager 字段
  app/main.py                        # lifespan + 路由注册 + 后台 MAC 捕获
  app/tools.py                       # vision 工具加 camera_id + 注入摄像头列表
  app/core/config.py                 # 删除 vision/ptz 属性访问
  app/schema/api_schemas.py          # camera 相关请求 schema
  config.example.json                # 删除 vision/ptz 段,补 cameras 示例(可选)
  frontend/src/views/ChatView.vue
  frontend/src/views/MonitorView.vue
  frontend/src/router/index.js
  frontend/src/components/SidebarNav.vue
  frontend/src/utils/api.js

删除:
  app/routes/ptz_routes.py           # 合并到 camera_routes
  config.json 的 vision/ptz/automation.camera_vl_display_enabled 段(迁移后)
```

---

## 12. 实施顺序(高层)

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | DB:cameras 表 + rules.camera_id 列 + 迁移逻辑 | — |
| 2 | CameraStream 参数化重构,拆 start/stop_display + 回调 | 1 |
| 3 | CameraDiscoveryService(多路化,基于单设备设计) | 1 |
| 4 | CameraManager(生命周期 + 双通道并发调度) | 2, 3 |
| 5 | PTZ/Vision/Automation/Rule 服务适配 camera_id | 1, 4 |
| 6 | camera_routes.py(合并 PTZ + Focus + Discovery) | 4, 5 |
| 7 | bootstrap/container/main 改造 + 后台 MAC 捕获 | 4, 6 |
| 8 | tools.py vision 工具适配 + 摄像头列表注入 | 4 |
| 9 | 前端 CameraSettingsView + useCamera | 6 |
| 10 | 前端 /camera 弹窗切换 + MonitorView 适配 | 9 |
| 11 | 配置清理(config.json/config.py) | 7 |
| 12 | 联调测试 | 全部 |

---

## 13. 验收标准

1. 现有单摄像头部署升级后,自动迁移成 `cameras` 表一条记录(ID 随机、名"默认摄像头"),RTSP/PTZ/运动/视觉参数全部保留,旧 config 段被删除。
2. 迁移后,该摄像头 RTSP 流、PTZ 云台、视觉关注项、自动化规则全部正常工作(无回归)。
3. 可在摄像头管理页添加第 2、3、4 路摄像头,每路独立配置来源/参数/PTZ,互不干扰。
4. `/camera` 弹窗可切换查看不同摄像头,同一时刻只跑当前路展示推理;切换时旧路停止、新路启动。
5. RTSP 路摄像头 DHCP 换 IP 后,自动触发该路 ONVIF 发现找回新 IP,RTSP + PTZ 同步恢复;其他路不受影响。
6. 四路同时触发运动自动化时,VLM 并发不超过 5,第 6 个排队;展示推理不受自动化配额影响。
7. `vision_chat` / `verify_condition` 工具能按用户自然语言(区域/名称)匹配到正确摄像头。
8. 删除摄像头时,关联规则的 `camera_id` 解绑(置空),关注项删除。
9. 单测覆盖:cameras 表迁移、CameraManager 生命周期、双通道并发调度(上限不超)、ONVIF 发现多路化(MAC 匹配/config 回写/重连通知)。

---

## Self-Review 结论

**1. Spec coverage(决策覆盖):**
- 决策 1(ID 方案)→ §3.1 cameras 表主键 + name + sort_order ✅
- 决策 2(MJPEG)→ §1.3 + §10 非目标(录像留 ZLM)✅
- 决策 3(VLM 并发)→ §2.3 双通道 + §6.1 Semaphore ✅
- 决策 4(推理分工)→ §2.2 职责 + §2.4 调用关系 ✅
- 决策 5(ONVIF)→ §5 整合 + §3.1 device_mac 字段 ✅
- 决策 6(迁移+删 config)→ §4 迁移 + §8 配置清理 ✅
- 决策 7(规则/关注项绑定)→ §3.2 rules.camera_id + vision_focuses KV 加字段 ✅
- 决策 8(定时任务不绑)→ §3.2 明确不加 ✅
- 决策 9(前端全量)→ §7 + §11 文件清单 ✅

**2. Placeholder scan:** 无 TBD/TODO;所有字段映射、路由、文件清单具体到列名和路径。

**3. Internal consistency:**
- §3.3 说密码明文存 DB,§4 迁移步骤 5/11 与之呼应(从 .env 读出写入 DB 再删 .env)✅
- §2.3 双通道(展示1+自动5),§6.1 CameraManager 两个 Semaphore 对应,§13 验收 6 验证 ✅
- §5 ONVIF find_camera(camera_id) 与 §3.1 device_mac 字段一致 ✅

**4. Ambiguity check:**
- §4 迁移"已迁移过的部署"判定标准已明确(cameras 表存在且非空),消除二次迁移歧义 ✅
- §6.3 规则端点保留在 rule_routes 而非嵌套进 camera_routes,消除"规则归属哪"的歧义 ✅
- §3.2 vision_focuses 保持 KV 不建表的决策已说明理由,消除"为什么 rules 建列 focus 不建表"的疑问 ✅

**5. Scope check:** 聚焦多路化 + ONVIF 整合 + 迁移 + 前端全量,单一实现计划可承载;录像/分布式/同屏显式列为非目标。
