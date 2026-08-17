# 自动化评估管道拆分——全局(定时/天气)规则独立静默循环

日期:2026-08-17
状态:已实施并验证(虚拟设备端到端)

## 背景与动机

原架构中,规则评估只有两个入口:dhash 运动事件触发和单一"定时器兜底"静默循环。
两者都评估**全部类型**规则(time/weather 走 chat LLM、vision 走 VL)。

问题:time/weather(非视觉)规则不消费帧,运动门控对它们没有语义意义,它们的
即时性只是"搭运动事件的便车";若摄像头无画面/无运动且静默间隔较长(默认 300s),
时间规则的触发精度最差达 5 分钟。

## 设计决策(与用户确认)

1. **非视觉规则不再由运动事件顺带评估**,只走自己的独立静默循环(默认 30s)。
2. **双独立开关**:视觉兜底与非视觉兜底各自有开关+间隔;关闭非视觉兜底后
   time/weather 规则将不再有任何评估来源(UI 已标注此后果)。
3. **拆分维度是 `rule.type`**(引擎路由维度),不是规则列表 UI 的 `camera_id`
   分组维度;高级设置文案与 TaskView 下拉叫法对齐:「全局(定时/天气)规则兜底」
   「摄像头(视觉)规则兜底」。
4. 边界行为变更(明示):挂在某摄像头下的时间类规则(camera_id 非空、type=time),
   旧行为只在该摄像头运动时被顺带评估,新行为归入非视觉循环按间隔精确评估。

## 架构

```
评估管道(改动后):

  dhash 运动事件(camera_stream → camera_manager)
    └─ evaluate(frames, camera_id, rule_types=("vision",))     [仅视觉,带帧]

  视觉静默兜底(AutomationAgent._silent_tick_loop,原定时器兜底)
    └─ 遍历各路取帧 evaluate(..., rule_types=("vision",))
       配置: automation.silent_eval_enabled / silent_eval_interval_seconds(默认300)

  非视觉静默兜底(AutomationAgent._nonvision_tick_loop,新增)
    └─ evaluate(frames=None, camera_id="", rule_types=("time","weather"))
       配置: automation.nonvision_silent_enabled / nonvision_silent_interval_seconds(默认30)
       不遍历摄像头、不占 camera_manager 并发闸,自带重叠保护。
```

`evaluate()` 的 `rule_types=None` 表示全部(向后兼容)。类型过滤放在过滤链
**最前面**(最廉价的检查),避免他管道规则先命中设备门控污染 `gated` 日志归因。

## 已知代价

条件未满足且设备门控未命中的 time/weather 规则,每 tick 每条一次 chat LLM 调用:
- 30s 间隔 ≈ 最坏 2880 次/天/条
- 10s 间隔 ≈ 最坏 8640 次/天/条

前端滑块描述已提示成本,由用户自行权衡。

## API 变更

- `GET /automation/status` 新增:`nonvision_silent_enabled`、
  `nonvision_silent_interval_seconds`、`nonvision_eval_count`。
- `POST /automation/silent` 请求体新增 `scope: "vision" | "nonvision"`
  (默认 vision,兼容旧调用方),按 scope 分派到对应循环与配置键。

## 验证记录(2026-08-17,虚拟设备,零真实设备)

- 单测:81 项相关测试全绿(含新增 rule_types 过滤/双循环/scope 分派/运动路径
  vision-only 断言);全量套件中 `test_admin_roles` 一项失败为存量问题
  (stash 后复现,与本改动无关)。
- 容器部署:重建镜像后 `AutomationAgent started (vision-silent=True/300.0s,
  nonvision-silent=True/30.0s)`。
- 端到端:启用存量 weather 规则「天气晴朗开灯」→ 非视觉 tick 评估
  (`1 chat-rule(s), 0 vision-rule(s), 0 frames`)→ chat LLM 命中 →
  `light.turn_on light.chuang_tou_deng` → HA → MQTT → 模拟器虚拟灯点亮
  (`← bedroom/light/set: {"state":"ON"}`)。
- scope 热切换:vision/nonvision 间隔各自落盘互不干扰;重启后保持。
- 日志归因:修复后运动触发流 `gated=1`(设备门控),非视觉流 `skipped=1`
  (类型过滤),两条管道节奏各自独立正确。

## 涉及文件

| 文件 | 改动 |
|---|---|
| `app/services/automation_service.py` | evaluate 加 rule_types 参数,类型过滤前置 |
| `app/agents/automation_agent.py` | 双循环/双开关/双防抖/双计数 |
| `app/services/camera_manager.py` | 运动触发 `_eval_one` 传 rule_types=("vision",) |
| `app/schema/api_schemas.py` | AutomationSilentRequest 加 scope |
| `app/routes/automation_routes.py` | status 双配置;silent 按 scope 分派 |
| `app/main.py` | 启动接线读新配置键 |
| `config.json` / `config.example.json` | automation 段新增双键 |
| `frontend/src/views/AdvancedView.vue` | 两组开关+滑块+摘要+双计数 |
| `tests/test_automation_agent.py` 等 | 更新旧断言+新增双管道用例 |
