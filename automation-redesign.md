# Aether 自动化规则重构方案（修订版）

> 修订说明：相比初版砍掉了不合理项——语义缓存（P2 整段）、`once` 一次性标志、`dhash_change_threshold` 重复字段、dhash 进缓存键、result=1 清缓存等；新增三类规则路由、设备状态门控、dhash 触发 3s 节流闸、dhash 与定时器统一降级。

## 目标

把自动化评估从「10s 轮询 + 推理完成双触发」改为「dhash 事件触发（节流）+ 定时器兜底 + 按规则类型路由 + 设备状态门控」，在**不丢「误操作自动恢复」能力**的前提下压低 LLM/VL 调用。

---

## 一、总览

| 优先级 | 功能 | 说明 |
|--------|------|------|
| P0 | dhash 事件触发 + 3s 节流 | 删 10s 轮询；dhash 运动触发 evaluate，trigger 自带 ≥3s 节流，防 0-result 规则被 300/min 轰 |
| P0 | 删 on_inference_done 双触发 | 只留 dhash 触发 + 定时器兜底两条入口 |
| P0 | 定时器兜底（静默推理） | 用户开关 + 间隔滑块(5s~3600s)，热切换立刻评估一次；dhash 阈值拉满即降级为纯定时器 |
| P0 | 摄像头视觉展示开关（解耦） | 只关 /camera 页面 VL 预览推理，不影响自动化；dhash 检测移出 recognizer 门控 |
| P0 | 规则冷却 config 可调 | 新增 `automation.default_cooldown_seconds`（默认 5），前端可调；per-rule 仍各自存 |
| P1 | 三类规则路由 | 创建时定死 type=time/weather/vision；time/weather 走 chat，vision 走 VL；混合归 vision |
| P1 | dhash 阈值可调 | 复用 vision.motion_threshold，不新增字段；滑块 1~`hash_size²`，最大=关 dhash 降级定时器 |
| P1 | 设备状态门控 | 动作蕴含目标态，执行前 cheap HA 查状态，已在目标态跳过整条规则（0 LLM） |

**砍掉：**
- ❌ 语义缓存 P2（向量库 / 影子模式 / 正式模式 / 缓存统计 / 误判率 DB）
- ❌ `once` 一次性标志（误操作后规则死掉，"炸"）
- ❌ `dhash_change_threshold`（与 vision.motion_threshold 重复）
- ❌ dhash 进缓存键

---

## 二、架构

```
定时器兜底 tick（可调间隔；dhash 阈值拉满时为主驱动）
    │
    ▼
dhash 运动检测（每 motion_check_interval=0.2s，复用 motion_threshold）
    ├─ moved=False → 无操作
    └─ moved=True → trigger_evaluate()  [自带 ≥3s 节流：复用 min_infer_interval]
                         │
                         ▼
              automation_service.evaluate()
                ├─ 遍历规则，各自 _in_cooldown(5s) 独立判断
                ├─ 按 rule.type 路由：
                │    ├─ time / weather → chat LLM（_evaluate_context_only，按时间+天气）
                │    └─ vision         → VL（evaluate_condition，带 frames）
                ├─ 设备状态门控（条件成立后、执行前）：
                │    动作蕴含目标态（close_cover→关 / turn_off→关 / set_temperature 26→26°C）
                │    先 cheap HA 查当前设备状态
                │    ├─ 已在目标态 → 跳过（0 LLM、0 action）
                │    └─ 不在目标态 → 才执行动作
                └─ 执行了动作 → update_trigger_time（武装冷却）
```

**降级路径**：dhash 阈值拉到 64 → `moved` 永不成立 → 仅靠定时器 tick 驱动（等价现状轮询，间隔用户选）。

**防误操作恢复**：设备状态门控保证"被人手动改了设备"会在下一周期被发现（状态≠目标→重新评估→重执行），不需要 `once`，不会"炸"。

---

## 三、后端改动

### 3.1 config.json automation 块

```json
"automation": {
    "eval_interval_seconds": 10,
    "silent_eval_enabled": true,
    "silent_eval_interval_seconds": 60,
    "default_cooldown_seconds": 5
}
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `silent_eval_enabled` | bool | true | 定时器兜底开关 |
| `silent_eval_interval_seconds` | int | 60 | 间隔(5~3600)，dhash 拉满时即轮询间隔 |
| `default_cooldown_seconds` | int | 5 | 规则冷却默认值，前端可调；per-rule 仍各自存 `cooldown_seconds` |

- `eval_interval_seconds` 废弃保留（兼容老部署）。
- dhash 阈值复用 `vision.motion_threshold`（已有，默认 15，不新增字段）；滑块范围 **1 ~ `vision.motion_hash_size`²**（默认 hash_size=16 → 1~256），**拉到最大(256) = `distance > 256` 永不成立 = 关 dhash、降级纯定时器**。值不会超过 256，所以**无需额外写"禁用"开关**，256 自然就是降级轮询档。`MotionDetector` 不动。
- dhash 触发节流复用 `vision.min_infer_interval_seconds`（已有，默认 3s）。
- 注：dhash 是 `hash_size²` 位（`motion_service.py:9-17`），默认 256 位、最大汉明距离 256。初版写的「1~64 / 拉 64 降级」是按 8 位哈希算的，默认 16 位下错。

### 3.2 app/agents/automation_agent.py 重写

删 `_tick_loop`、`_eval_interval`、`_last_eval_at`。新结构：

- `trigger_evaluate()`：dhash 回调入口；**自带 ≥3s 节流**（`_last_trigger_at` + `_min_trigger_interval`，复用 `min_infer_interval`）。3s 内的重复 trigger 丢弃——这是防 0-result 规则被 300/min 轰的关键（冷却只在 result==1 后才武装，挡不住一直返回 0 的规则）。
- `_run_evaluation_cycle()`：调 `automation_service.evaluate(frames=frames)`（不变）。
- `_silent_tick_loop()`：定时器兜底，按 `silent_eval_interval` 周期 evaluate。
- `set_silent_interval(seconds)`：热切换间隔，**加防抖**（滑块拖动期间不刷屏，松手/停止后生效一次）；切换后立刻评估一次。
- `set_silent_enabled(enabled)`：开关定时器。
- `_start_silent_tick()` / `_stop_silent_tick()`：生命周期。

不动：`AutomationService.evaluate()` 的 `_in_cooldown` 逻辑（仅默认值改 5）。

### 3.3 app/camera_stream.py

新增：
- `set_on_automation_trigger(callback)`：注册 dhash 运动回调。
- `set_camera_vl_display_enabled(enabled)` + `_camera_vl_display_enabled`：只门控 /camera 页面 `classify_frame` 预览推理。

**关键改 `_maybe_schedule_inference`**：把 dhash 检测 + 自动化触发移到 `if not self._recognizer.enabled: return` 之前，关掉视觉展示不影响自动化：

```python
def _maybe_schedule_inference(self, frame):
    now = time.time()
    if now - self._last_motion_check < self._motion_check_interval:
        return
    self._last_motion_check = now
    moved, distance = self._motion.assess(frame)
    # 自动化触发：moved 即触发，不受视觉展示开关影响
    if moved and self._on_automation_trigger is not None:
        self._on_automation_trigger()
    # /camera 展示推理：受视觉展示开关门控
    if not self._camera_vl_display_enabled:
        return
    # ……原有 infer_busy / min_infer_interval / interactive_priority / 调度 classify_frame
```

不动：MJPEG、MotionDetector、视觉推理本体、心跳（`max_idle_interval`）。

### 3.4 app/main.py

- **删 `_on_inference_done → trigger_evaluate`**（main.py:489-496）这条双触发。`set_on_inference_done` 保留空实现或移除。
- `camera_stream.set_on_automation_trigger(automation_agent.trigger_evaluate)` 注册 dhash 回调。
- 从 config 读 `silent_eval_*`，注入 AutomationAgent。
- 注入视觉展示开关状态。

### 3.5 app/routes/automation_routes.py（新建）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/automation/status` | GET | 所有自动化配置 |
| `/api/automation/silent` | POST | `{enabled, interval_seconds}` 热切换 |
| `/api/automation/dhash-threshold` | POST | `{threshold}` 调 motion_threshold（与摄像头共享） |
| `/api/automation/vision-recognizer` | POST | `{enabled}` 开关 /camera 视觉展示 |

砍掉原计划的 `/semantic-cache` 与 `/semantic-cache/stats` 两个端点。

### 3.6 app/services/automation_service.py

- **按 type 路由**（替代全局 `use_context_only`）：每条规则按自身 `type` 决定走 chat 还是 VL，不再"帧在则全部走 VL"。time/weather 规则即使有帧也走 chat（省下白送的 VL）。
- **设备状态门控**：条件成立后、执行动作前，按动作推导目标态（domain/service/entity_id/data），cheap HA 查当前状态；已在目标态→跳过（不执行、不重复）。不在→才执行。
- 冷却默认 10→5。

### 3.7 规则 type 字段（schema + 建规则 + 迁移）

- `app/schema/api_schemas.py`：规则加 `type: str = "vision"`；cooldown 默认 10→5。
- `app/services/rule_service.py`：`build_rule` 的解析 prompt 加「输出 type 字段，取值 time/weather/vision，沾视觉一律 vision」；cooldown 默认 10→5。
- `app/services/rule_registry_service.py`：`AutomationRule` 加 `type` 字段；cooldown 默认 10→5；`load_from_db` 老规则无 type → 按 condition 猜（含「看/画面/桌/人/坐/站」等视觉词→vision；含「雨/温/天气」→weather；含「点/时/早/晚/夜」→time；兜底 vision）。

### 3.8 冷却改为 config 可调默认值（不再硬改 10→5）

新增 `automation.default_cooldown_seconds`（默认 5），4 处改为读 config 默认，前端在「自动化」modal 可调。per-rule 仍各自存 `cooldown_seconds`（建规则时定，互不影响）。

| 文件 | 改法 |
|------|------|
| `app/schema/api_schemas.py` | `cooldown_seconds: int` 默认改读 config（或 schema 默认 5） |
| `app/services/rule_service.py` | `setdefault("cooldown_seconds", get_config("automation.default_cooldown_seconds", 5))`；`_fallback_rule` 同步 |
| `app/services/rule_registry_service.py` | `AutomationRule` + `add_rule` + `load_from_db` 缺省读 config 默认 |
| `app/services/automation_service.py` | `rule.get("cooldown_seconds", get_config("automation.default_cooldown_seconds", 5))`（两处） |

注：改 config 默认只影响**新建/无显式 cooldown 的规则**；老规则已存的 `cooldown_seconds` 不变（per-rule 持久化）。

---

## 四、前端

### 4.1 AdvancedView.vue 加「自动化」卡片 + modal

跟现有 weather/exa/vision/ptz/ha/unique/keys 一个风格：config-grid 加一张「自动化」卡片，点开弹 modal。modal 内控件：

| 控件 | 说明 |
|------|------|
| 定时器兜底 | 开关 + 间隔滑块(5s~3600s) |
| dhash 阈值 | 滑块 1~`motion_hash_size²`（默认 1~256），最大=关 dhash 降级定时器；复用 `vision.motion_threshold`（与摄像头预览共享，注明） |
| 默认冷却 | 滑块(1~3600s)，写 `automation.default_cooldown_seconds`；注明只影响新建/无显式 cooldown 的规则 |

视觉展示开关**不放这里**（放 /camera 页面，见 4.2）。
砍掉原计划的「语义缓存开关 / 相似度阈值 / 缓存统计」三个控件。

### 4.2 ChatView.vue /camera 页面加视觉展示开关

在 camera 弹窗 camera-stats 上方加视觉展示推理开关（调 `/api/automation/vision-recognizer`），不进 Advanced。关掉只停 /camera 的 VL 预览推理，自动化不受影响。

---

## 五、改动文件清单

| # | 文件 | 操作 | 行数 |
|---|------|------|------|
| 1 | `config.json` | 加 `silent_eval_*` + `default_cooldown_seconds` | ~4 |
| 2 | `app/agents/automation_agent.py` | 重写 | ~110 |
| 3 | `app/camera_stream.py` | dhash 解耦 + 展示开关 | ~30 |
| 4 | `app/main.py` | 删双触发 + 注册回调 + 注入 | ~15 |
| 5 | `app/routes/automation_routes.py` | 新建 | ~60 |
| 6 | `app/services/automation_service.py` | type 路由 + 状态门控 + cooldown | ~50 |
| 7 | `app/schema/api_schemas.py` | type 字段 + cooldown 10→5 | ~2 |
| 8 | `app/services/rule_service.py` | build_rule 输出 type + cooldown | ~15 |
| 9 | `app/services/rule_registry_service.py` | type 字段 + 迁移 + cooldown | ~20 |
| 10 | `frontend/.../AdvancedView.vue` | 自动化区块 | ~30 |
| 11 | `frontend/.../ChatView.vue` | 视觉开关 | ~15 |

合计约 **~350 行**（砍了 P2 整块，但加了路由 + 状态门控）。

---

## 六、成本画像（修订后）

以「雨天关窗帘」(weather 类，1 条) 为例：

| 场景 | 调用 |
|------|------|
| 下雨 + 窗帘开 | 1 chat（判条件）+ 关窗帘 |
| 窗帘保持关着 | **0 调用**（设备状态门控跳过） |
| 中途被人拉开 | 1 chat + 重关（自动恢复） |
| 静止无运动 | 定时器 1 chat / 间隔（默认 60s） |

视觉类规则：dhash 触发（≥3s 节流）→ 设备已在目标态则跳过 → 不在才 VL。稳态同样趋近 0。
