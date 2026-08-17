# 小爱插件：自动检测接入 + 直通修复 + 静默失败守卫

日期：2026-08-17
状态：已批准（方案 A：插件进程内懒检测）

## 背景与根因

2026-08-17 排查"小爱完全没反应"实锤三个独立事实：

1. **实体 ID 事故（已应急修复）**：提交 `ae7adc7` 把 manifest 默认实体改成占位符
   `media_player.xiaoai_pro`，插件拼出不存在的 notify 实体；HA 对不存在的 notify
   实体返回 **200 空列表**——静默假成功，无任何日志。应急方案是往
   `integration.host_configs.xiaoai` 写真实实体 + 重启容器（管理页配置经
   `AETHER_PLUGIN_CONFIG` 注入插件进程，`stdio_runtime` 覆盖 manifest 默认值）。
2. **直通模式从未可用**：xiaomi_home 的 `notify.py` 把 message 当 **YAML 解析成
   action 参数列表**。`play_text` 只有 1 个参数，纯文本能过；`execute_text_directive`
   需要 `[文本内容(str), 指令静默执行(bool)]` 两个参数，纯文本解析不出列表 →
   xiaomi_home 只写一行 HA 日志就 return，HA 照样返回 200 并更新 notify 实体时间戳。
   实测 `json.dumps([text, false])`（JSON 是合法 YAML）小爱正常执行。
3. **execute_mode 是死配置**：`plugin.py` 读入 `self._execute` 后从未使用。

## 需求

- 插件装进来即用：不配置也能自动找到 HA（xiaomi_home 集成）里的小爱音箱并接入；
  米家改名不影响识别（识别基于 MIoT 规格后缀，与名字无关）。
- 多台小爱音箱时不擅自选择：报错列出候选，引导用户在集成管理页配置。
- 显式配置 > 自动检测；配置错了要报错，不静默回退。
- 所有失败必须在聊天界面可见（杜绝静默假成功）。

## 设计

### XiaoAiResolver（plugin.py 内，懒检测）

- 首次 speak/route/interrupt 时经 `host.ha.get_states()`（返回 `{"states":[...]}`）解析；
  成功结果缓存；`invalidate()` 后下次重扫（应对换音箱/实体变更）；`asyncio.Lock`
  防并发重复扫描；只缓存成功。
- 显式配置（管理页 entity_id 非空，指 media_player 实体）：按 MIoT 后缀推导两个
  notify 实体并在 states 里校验；校验失败抛错（不回退自动检测）。
- 自动检测：扫 `notify.<slug>_execute_text_directive_a_5_5` 且同 slug 的
  `notify.<slug>_play_text_a_5_1` 也存在。后缀来自 MIoT 规格 action 编号
  （siid=5/aiid=1、siid=5/aiid=5），跨型号稳定、米家改名不影响。
  - 恰好 1 台 → 接入（`media_player.<slug>` 存在则配对，缺失则 interrupt 降级）
  - 0 台 → "未发现小爱音箱（需要 xiaomi_home 集成接入小爱音箱）"
  - 多台 → 列出全部候选，提示去管理页配置

### 消息序列化（直通修复）

- `play_text`：`json.dumps([text], ensure_ascii=False)`
- `execute_text_directive`：`json.dumps([text, False], ensure_ascii=False)`
  （`False` = 非静默执行，小爱有声反馈）
- JSON 是合法 YAML、任意引号/换行安全转义；顺带免疫纯文本 "on"/"yes" 被 YAML
  强转布尔的边角。

### 空结果守卫

- `call_service` 返回 `[]` == 实体不存在 → 返回 `{"error": ...}` + `invalidate()`。
- HA 调用异常 → 同样包装成 error dict + `invalidate()`。

### speak / route / interrupt

- speak：串行锁内先 resolve → 按 `execute_mode` 选实体（激活死配置）→ 列表 payload → 守卫
- route：resolve → execute 实体 → 列表 payload → 守卫；成功返回
  `{"ok": True, "executed": text, "speaker": slug}`
- interrupt：resolve → media_player 存在才 `media_stop`，缺失跳过并记警告
- 失败一律返回 `{"error": "<中文原因>"}`（聊天界面 `_handle_direct` 直接展示）

### manifest.json

- `entity_id`：default `""`、required `false`、label "小爱实体ID（留空自动检测）"
  （空 = 自动检测；非空 = 用户显式指定。配置机制会把用户值合并进 default，
  插件以"非空"判定显式配置）

## 测试

- 更新 `tests/integrations/test_xiaoai_plugin.py`、`test_xiaoai_router.py`：
  消息断言改 JSON 列表；实体推导断言改 Resolver 行为（原测试把纯文本 bug 固化成了预期）。
- 新增 Resolver 测试（mock states）：单台自动接入 / 多台报错列候选 / 零台报错 /
  显式配置通过与失败（失败不回退）/ media_player 缺失 interrupt 降级 /
  `[]` 守卫 error+invalidate / "on" 类文本不被强转。
- 回归：`pytest tests/integrations/ tests/test_plugin_config.py`

## 部署与验证

1. 用户先处理工作区未提交改动（提交或暂存）。
2. `docker compose build aether && docker compose up -d aether`（integrations/ 未挂载，
   代码打进镜像）。
3. 部署时清掉 `config.json` 的 `host_configs.xiaoai`（应急写入），让生产直接走自动检测。
4. 验证：app.log 出现"自动检测到小爱音箱"；UI 转播出声；直通模式"播放音乐"实际放歌；
   HA 日志无新 `invalid action params`。
5. 回滚：`git revert` + 重建；或恢复 host_configs 显式配置。

## 不做的事（YAGNI）

- 不做多音箱全屋广播
- 不改宿主框架（stdio_runtime / plugin_process / integration_layer 零改动）
- 管理页不做动态设备下拉（错误信息列出候选可复制即够）
