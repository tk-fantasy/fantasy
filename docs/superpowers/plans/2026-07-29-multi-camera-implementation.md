# 多摄像头改造 — 可直接落地实施计划

> **状态**:待实施(等摄像头到位)。依据 `docs/superpowers/specs/2026-07-29-multi-camera-design.md`(已批准 spec)。本计划补齐 spec 未覆盖的实施细节(已用「⚠️ 实施注意」标注),并给出精确到 file:line 的改动点,落地时照此执行即可。
>
> **核心**:`camera_id` 参数化全局单例 → 双通道 VLM 并发(Semaphore 1+5)。
>
> **已确认决策**:vision_focuses per-camera 拆分;`/api/health`、`/api/state` 改 per-camera + 保留兼容。
>
> **交付节奏**:一次性出完整方案(后端 10 步 + 前端 2 步),落地时按步 commit、每步可独立验证(TDD)。前后端可分两个 PR(后端先合,前端跟进)。

---

## Step 1 — DB:`cameras` 表 + `rules.camera_id` 列 + 幂等迁移

**文件**:`app/core/database.py`、`tests/test_database.py`、`tests/conftest.py`

1. 在 `database.py:39` 的 `executescript` 加 `CREATE TABLE IF NOT EXISTS cameras (...)`(schema 照 spec §3.1,密码明文列)。字段名严格用 spec §4 字段映射表的 cameras 列名。
2. `rules` 加 `camera_id`:`database.py:99` 的 `_ensure_column` 模式追加 `_ensure_column("rules", "camera_id", "TEXT DEFAULT ''")`(与现有 user_id 迁移同模式)。
3. 迁移逻辑加进 `init()`(幂等),**⚠️ 实施注意 — 幂等判据改用 KV 标记**(spec §4 用"表非空",但有"全新部署手动删空所有摄像头 + 残留 env config"误判风险):
   - `db.kv_get("cameras_migrated")` 为 `"1"` → 已迁移,跳过
   - 否则检测 config.json 是否有旧 `vision`/`ptz`/`automation.camera_vl_display_enabled` 段;有 → 按 spec §4 步骤 1-11 迁移(生成 `cam_<6位>`,名"默认摄像头",读 config 各字段,从 `.env` 读 `RTSP_PASSWORD`/`PTZ_PASSWORD` 写 DB,旧规则/关注项回填 camera_id,**删 config 三段 + 删 .env 两密码**)
   - 完成后 `db.kv_set("cameras_migrated", "1")`
4. `vision_focuses` KV 每条加 `camera_id` 字段(本步只做字段结构,功能拆分在 Step 2):迁移时把现有数组每条补 `camera_id=新默认ID`。
5. **测试**:`test_database.py` 用 `patch("app.core.database.DB_PATH", tmp)` 隔离,测:① 全新部署跳过迁移;② 老部署迁移一条记录、字段映射正确;③ 二次 init 幂等(KV 标记命中);④ 删空后不误迁移。
6. conftest 的 `_patch_config`(`conftest.py:29`,autouse)里 test_config dict 的 `vision`/`ptz` 段**保留**(迁移测试要用),迁移成功测试里改成读 cameras 表断言。

---

## Step 2 — `CameraStream` 参数化重构 + per-camera focuses

**文件**:`app/camera_stream.py`、`app/services/vision_service.py`、`tests/test_camera_stream.py`(新建)

1. 构造函数(`camera_stream.py:64`)改为 `__init__(self, camera_id, config: dict, vision_service, motion_service=None, on_automation_trigger=None, discovery_service=None)`。`config` 是 `cameras` 表行映射的 dict。`__init__` 内 22 处 `get_config("vision.*")`(`camera_stream.py:83-127`)全部改读 `config` dict(键名对应 spec §4 映射表)。RTSP 4 字段(`camera_stream.py:488-518`)同样改读 config。
2. 加 `self.camera_id = camera_id`;`CameraState`(`camera_stream.py:30`)加 `camera_id` 字段,`get_state()`(`:242`)自动经 `asdict` 暴露。
3. **展示开关重构**:`set_camera_vl_display_enabled`(`:324`,两调用点 `main.py:506` + `automation_routes.py:87`)→ 改名 `set_display_enabled(enabled)`。门控点在 `:765`(`_maybe_schedule_inference`),语义不变(只是改 setter 名)。⚠️ spec 说改 `start_display`/`stop_display`,但探索发现门控是布尔标志(`:765`)+ setter 写配置,改成两个布尔方法更贴合现状,无需拆线程。
4. **运动触发改回调**:触发点 `:758-762` 调 `self._on_automation_trigger()`。改为 `on_automation_trigger` 接收 `camera_id` —— 触发时传 `self.camera_id`。注入方法 `set_on_automation_trigger`(`:316`)签名改为接收 `callback: Callable[[str], None]`。
5. **discovery 注入**:`set_discovery_service`(`:346`)不变,但 worker 内 `:608` 的 `find_and_apply()` 调用改为 `find_and_apply(self.camera_id)`(配合 Step 4)。
6. **per-camera vision_focuses**(⚠️ 选了拆分):`VisionService`(`vision_service.py:32`)的 `_vision_focuses: list[dict]`(`:35`)改为 `_vision_focuses: dict[str, list[dict]]`(按 camera_id 分桶)。方法 `get_vision_focuses/add_focus/update_focus/delete_focus/load_focuses`(`:64-93`)全部加 `camera_id` 参数。消费点 `dispatcher.py:376` 取 focus 时带当前 camera_id(Step 9 处理 dispatcher 拿 camera_id 的逻辑)。
7. **测试**(⚠️ 现无 `test_camera_stream.py`):新建,Mock `vision_service`,测:① 构造从 config dict 读参数;② `set_display_enabled` 门控;③ 运动触发回调带 camera_id;④ per-camera focus 分桶互不串。

---

## Step 3 — `CameraDiscoveryService` 多路化(`camera_id` 参数化)

**文件**:`app/services/camera_discovery_service.py`、`tests/test_camera_discovery_service.py`

1. 所有方法加 `camera_id` 参数,MAC/子网/凭证从 `cameras` 表对应行读(改用 `db.cameras_get(camera_id)` —— Step 5 在 database.py 加 CRUD),不再读全局 config:
   - `find_camera(camera_id)`(`:217`):读该路 `device_mac` + `ptz_ip` 推子网
   - `apply_found_ip(camera_id, new_ip)`(`:293`):更新**该路** `rtsp_url` host + `ptz_ip`,通知该路 worker + PTZ 重连
   - `capture_mac_on_startup(camera_id)`(`:345`)、`find_and_apply(camera_id)`(`:385`)同理
2. ⚠️ `find_camera` 内 9 处 `get_config("vision.*")`(`:84/241/248/254/262/327/369/371/378`)和 PTZ 8 处全部改读 cameras 行。`infer_subnet`/`normalize_mac`/`_mac_match`/`_scan_ports`/`_probe_candidate` 等纯函数不变。
3. **测试**:现有 `test_camera_discovery_service.py` 的 ONVIF mock 模式(`_make_cam` + `patch("onvif.ONVIFCamera")`)保留,每个测试改用 cameras 表行 mock 替代改 config,断言多路独立发现。

---

## Step 4 — `CameraManager`(生命周期 + 双通道并发调度)

**文件**:`app/services/camera_manager.py`(新建)、`tests/test_camera_manager.py`(新建)

1. 按 spec §6.1 实现,核心两个 Semaphore:`_display_sem = Semaphore(1)`、`_auto_sem = Semaphore(5)`(常量可配)。
2. `_streams: dict[str, CameraStream]`、`_active_display_id: str | None`。
3. 方法:`initialize()`(从 DB 加载所有 enabled,每路 new CameraStream + 注入 + start)、CRUD(`create/update/delete_camera`)、`enable_display/disable_display`(切换展示通道,旧的停新的启)、`get_frame/get_recent_frames/mjpeg_generator/get_state`(都带 camera_id)、`request_automation_eval(camera_id, frames)`(拿 `_auto_sem` 名额跑该路评估)、`request_tool_inference(camera_id, prompt, frames)`(共享 `_auto_sem`)、`list_cameras()`(供工具注入)。
4. ⚠️ **worker→manager 跨线程桥接**:CameraStream 的 `on_automation_trigger(camera_id)` 是 worker 线程同步调用,但 `request_automation_eval` 是 async 且要拿 Semaphore。沿用现有模式 —— CameraStream 内部用 `run_coroutine_threadsafe(manager.request_automation_eval(camera_id), loop)` 投主循环(与推理投递 `:302` 同机制)。manager 需注入 loop。
5. ⚠️ **GIL 竞争**:`run_coroutine_threadsafe` 已保证 httpx 等待释放 GIL,但 N 路 OpenCV 解码仍抢 GIL。落地后实测 4 路 FPS,若采集饿死,考虑把 `_worker` 的 imencode 工作下沉或降帧。此点 Step 12 联调验证。
6. **测试**:Mock CameraStream,测:① enable_display 切换(旧的 stop);② 双通道上限(Semaphore(1)/(5) 超限排队);③ 并发不超上限;④ CRUD 转发 DB。

---

## Step 5 — 服务适配 `camera_id`(PTZ/Vision/Rule + DB CRUD)

**文件**:`app/services/ptz_service.py`、`app/services/rule_service.py`、`app/services/automation_service.py`、`app/core/database.py`

1. **DB CRUD**(先于服务):`database.py` 加 `cameras_all/cameras_get/cameras_insert/cameras_update/cameras_delete`。
2. **ptz_service**(`:55` 单例)参数化:现状是全局单例读 `ptz.*`(6 处 `:71-118`)。改为按 `camera_id` 管理 —— 要么 `dict[camera_id → PtzService]`,要么 PtzService 加 `camera_id` + 配置从 cameras 行读。`notify_ip_changed`(`:120`)已存在,保留。
3. **automation_service**:`evaluate(frames)`(`:35`)→ `evaluate(frames, camera_id)`,规则按 `camera_id` 过滤(`_in_cooldown`/`_device_already_in_target` 逻辑不变,只是规则集按摄像头取)。
4. **rule_service**:CRUD 支持 `camera_id`(`_parse_ha_catalog` 等不变,LLM 解析规则时带 camera_id 上下文)。
5. ⚠️ spec §6.5 提到 AutomationAgent 触发带 camera_id,但这已在 Step 4 的 manager 回调里解决(manager 按 camera_id 取规则评估),AutomationAgent 本身可保持 `camera_id` 透传。
6. **测试**:更新 `test_ptz_service.py`(ONVIF mock 模式不变,改参数)、`test_automation_service.py`(按 camera_id 过滤规则)。

---

## Step 6 — `camera_routes.py`(合并 PTZ + Discovery + Focus + 状态)

**文件**:`app/routes/camera_routes.py`(新建)、`app/main.py`、`app/routes/settings_routes.py`、`app/routes/ptz_routes.py`、`app/routes/discovery_routes.py`、`app/routes/mcp_routes.py`

1. 新建 `camera_routes.py`,路由按 spec §6.3(`/api/cameras` CRUD + `/api/cameras/{id}/video_feed` + `display/enable|disable` + `state` + `ptz/*` + `focuses/*` + `discovery/*`)。规则端点**保留在 rule_routes**(spec §6.3 明确,带 `?camera_id=` 查询过滤)。
2. **vision-focus 端点迁移**:从 `settings_routes.py:717-789` 物理移除 6 个 handler(连续块),移到 camera_routes。导入镜像 `settings_routes.py:9-36`(`Depends`/`get_container`/`Database`/Vision schema)。
3. **MJPEG 端点迁移**:`mcp_routes.py:136` 的 `mjpeg_generator()` 改为 `camera_routes` 的 `/api/cameras/{id}/video_feed`,调 `manager.mjpeg_generator(camera_id)`。
4. **main.py 注册**:`main.py:584-585/604-605` 删 ptz_router/discovery_router 导入和注册,加 `camera_router`。vision-focus 因路径是 `/vision/...` 不与 `/settings/...` 冲突,前端 URL 不变。
5. ⚠️ **旧路由废弃**:探索确认 `discovery_routes.py`、`ptz_routes.py` 可直接删(app 侧仅 main.py 引用)。**但 3 个测试文件需重定向**:`test_discovery_routes.py`、`test_ptz_config.py` 的 `from app.routes import X` + `patch.object(X,...)` 改向 `camera_routes`;`test_camera_discovery_service.py`/`test_ptz_service.py` 是服务级测试,不动。
6. **测试**:新建 `test_camera_routes.py`,用 `test_routes_extra._mock_container`(`:17`)模式,设 `c.camera_manager` + `c.vision_service`。

---

## Step 7 — bootstrap / container / main 改造 + 后台 MAC 捕获

**文件**:`app/bootstrap.py`、`app/container.py`、`app/main.py`

1. **container.py**(`:47`):字段 `camera_stream` → `camera_manager`(CameraManager)。`init_container`(`:127`)对应改。
2. **bootstrap.py**(`:44`):`CameraManager(...)` 替代 `CameraStream(...)`。`initialize` 在 lifespan 跑(需 event loop + DB 已 init)。
3. **main.py lifespan**:替换 12 处 camera_stream 引用(`:430/452/470/498/501/504/506/507/554`):
   - `:430` ToolDeps 传 `camera_manager`
   - `:452` Dispatcher 传 manager(Dispatcher 内 `:198/212/423/466` 的 `get_state` 改为按需 camera_id 或默认路)
   - `:470` AutomationAgent 传 manager
   - `:498-507` 注入改为 `manager.initialize()`(内部每路 set_event_loop/set_discovery_service/set 回调/start)
   - `:554` `manager.stop()`(遍历各路 stop)
   - `:511-517` 后台 MAC 捕获改为遍历所有 `discovery_enabled && device_mac==""` 的路
4. ⚠️ **`automation_routes.py` 的全局滑块**(`:73-125` dhash/vision-recognizer):改 per-camera,带 camera_id 路径参数。
5. **测试**:更新 `test_routes_extra._mock_container`(`:25`)的 `c.camera_stream` → `c.camera_manager`。

---

## Step 8 — `tools.py` vision 工具适配 + 摄像头列表注入

**文件**:`app/tools.py`、`app/mcp/local_mcp_servers.py`

1. **工具加 `camera_id` 参数**:`tools.py:69`(`vision_chat` 的 `get_latest_frame`)和 `:217`(`create_verify_condition_handler`)改为带 camera_id,经 `manager.get_frame(camera_id)` / `get_latest_frame(camera_id)`。
2. **`local_mcp_servers.py:119/161`**:`create_verify_condition_handler` 签名加 camera_id,`:161` 的 `camera_stream.get_latest_frame()` 改 manager 按需取。
3. **摄像头列表注入**(⚠️ spec §6.4,且是工具描述动态注入的坑):运行时从 `manager.list_cameras()` 注入工具描述。**关键**:工具描述在 `build_chat_agent` 时静态绑定 —— 摄像头增删后必须触发 `_rebuild_agent`(main.py 现有 `_rebuild_lock` 机制),`create_camera/update_camera/delete_camera` 成功后调一次。或改用工具内动态查 manager(每次调用读当前列表),避免 rebuild。
4. **camera_id 三级 fallback**(spec §6.4):用户指定 → `/camera` 弹窗 `manager._active_display_id` → 第一个 enabled。
5. **测试**:`test_local_mcp_servers.py:85/121` 的 mock 更新;测 camera_id fallback + 列表注入。

---

## Step 9 — dispatcher / automation_agent 取 camera_id 上下文

**文件**:`app/agents/dispatcher.py`、`app/agents/automation_agent.py`

1. **dispatcher**:`:376` 取 focus 改为 `vision_service.get_vision_focuses(camera_id)`(配合 Step 2 per-camera)。⚠️ dispatcher 需知道当前 camera_id —— 从用户消息/工具调用上下文推断,或用默认路。`:423/466` 的 `get_state()` 改 `manager.get_state(camera_id)`。
2. **automation_agent**:`:189` 的 `get_recent_frames` 改为 manager 按 camera_id 取(回调里已带 camera_id)。
3. ⚠️ 这步是 Step 2/4/8 的收口,确保 camera_id 在 manager→agent→dispatcher→prompt 链路一致传透。
4. **测试**:更新 `test_dispatcher.py:30`、`test_automation_agent.py:207` 的 camera mock。

---

## Step 10 — `/api/health`、`/api/state` per-camera + 兼容

**文件**:`app/main.py`

1. ⚠️ 选了"改 per-camera + 保留兼容":
   - `/api/health`(`:730`)、`/api/state`(`:753`)返回**主摄像头**(第一个 enabled)状态,避免 /camera 弹窗外的前端引用崩。
   - 另加 `/api/cameras/{id}/state` 查指定路(已在 Step 6 camera_routes)。
2. `CameraStateModel` schema 加 `camera_id` 字段(Step 2 已加到 CameraState)。
3. **测试**:`test_http_smoke.py:46` 验证 health 路由仍可达。

---

## Step 11 — 前端:CameraSettingsView + useCamera

**文件**:`frontend/src/views/CameraSettingsView.vue`、`frontend/src/composables/useCamera.js`、`frontend/src/router/index.js`、`frontend/src/components/SidebarNav.vue`、`frontend/src/utils/api.js`

1. 照 spec §7.1 的卡片列表 + 编辑面板 mockup(已并入 specs)实现。区域下拉调 HA `/api/areas`。
2. `useCamera.js` 封装 cameraAPI(CRUD + display 控制 + focuses)。
3. 路由 `/cameras`,侧栏入口。

---

## Step 12 — 前端:`/camera` 弹窗切换 + MonitorView 适配 + 联调

**文件**:`frontend/src/views/ChatView.vue`、`frontend/src/views/MonitorView.vue`

1. `/camera` 弹窗顶部加切换标签(spec §7.2 mockup),`activeCameraId` 驱动 video_feed + PTZ + 状态。
2. VLM 调用控制:打开→`display/enable(当前)`;切换→`disable(旧)`+`enable(新)`;关闭→`disable`。
3. **联调验证**(spec §13 验收):① 老部署升级自动迁移一条记录、参数保留;② 加 2-4 路互不干扰;③ 弹窗切换只跑当前路展示推理;④ RTSP 路换 IP 自动发现找回;⑤ ⚠️ **4 路同时运动触发 VLM 并发 ≤5,展示推理不受影响**;⑥ 工具按区域/名称匹配;⑦ 删除摄像头解绑规则、删 focus。
4. ⚠️ **GIL/FPS 实测**:4 路全开时观察采集 FPS,确认不饿死(Step 4 留的验证点)。

---

## 落地注意事项(spec 未覆盖,已并入上面相应步骤)

1. **迁移幂等用 KV 标记**(`cameras_migrated`)而非 spec 的"表非空"(Step 1)—— 防全新部署删空 + 残留 env 误判。
2. **全局端点保留兼容**(Step 10)—— 防 /camera 弹窗外的前端引用崩。
3. **工具描述动态注入要 rebuild agent**(Step 8)—— 摄像头增删后调 `_rebuild_agent`,或改工具内动态查 manager。
4. **3 个测试文件重定向**(Step 6)—— `test_discovery_routes`/`test_ptz_config` 改向 camera_routes;服务级测试不动。
5. **GIL 竞争实测**(Step 4/12)—— N 路 OpenCV 解码抢 GIL,落地后验证 FPS。

---

## 每步 commit 建议(落地时)

每步独立 TDD + commit,前缀 `feat:`/`test:`/`refactor:`,中文描述。Step 1-10 后端,Step 11-12 前端。前后端可分两个 PR(后端先合,前端跟进),降低单 PR 体积。
