# Aether Conversation Agent（HA 自定义集成）

把 Aether 接成 Home Assistant Assist 的对话引擎——**入口归 HA，脑子归 Aether**：
wake word / STT / TTS 全走 HA Assist 管线，识别出的文本转发给 Aether 的
`POST /api/assist/chat`，由 Aether 理解并执行（设备控制/摄像头/自动化/闲聊），
回复交给 HA 管线播报。

## 部署

1. 本目录（`custom_components/aether_conversation/`）随 Aether 仓库分发；
   Aether 的 `aether-ha` 容器挂载 `ha_config/`，组件已在 HA 配置目录里。
2. Aether 侧设置 `APP_TOKEN` 环境变量（机器对机器令牌）并重启 Aether。
3. 重启 HA（组件首次被发现需要重启）。
4. HA「设置 → 设备与服务 → 添加集成 → Aether Conversation Agent」，填：
   - Aether 地址：如 `http://aether:8000`（容器网络内可达地址）
   - API Token：Aether 的 `APP_TOKEN` 值
5. HA「设置 → 语音助手」里把对话引擎（conversation agent）选成 **Aether**。
6. 对着 Assist 说「打开客厅灯」，Aether 会理解并经 HA 执行。

## 行为说明

- Aether 不可达时回固定话术（语音链路不硬失败），恢复后自动续上。
- `conversation_id` 映射 Aether 会话（`assist_<id>`），同一 HA 对话上下文连续。
- 修改地址/Token：集成条目「配置」进选项流，无需删除重建。

## 服务：`aether_conversation.fire_rule`（委托触发回调）

Aether 会把时间/天气类自动化规则的触发时机委托给 HA 原生自动化（精确定时器 /
状态事件，运行时零 LLM），动作固定调本服务把 `rule_id` 交回 Aether 执行：

```yaml
action: aether_conversation.fire_rule
data:
  rule_id: "规则id"
```

服务由 Aether 创建的委托自动化自动引用，一般无需手写；集成未配置（地址/Token
缺失）或 Aether 不可达时仅记 HA 日志、不硬失败。组件卸载时服务一并注销。
Aether 侧判断「能否委托」靠探测本服务是否已注册（组件加载且配置完成才会注册）。

