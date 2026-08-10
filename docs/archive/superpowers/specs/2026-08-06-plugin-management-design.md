# 插件管理页面 设计文档

**日期**: 2026-08-06
**状态**: 待评审

---

## 1. 背景与目标

用户在 `/chat` 输入 `/plugin` → 跳转插件管理页面，可：
- 查看所有插件 + 状态（运行中/崩溃/禁用）
- 启用/禁用插件（不删文件，禁用的重启不加载）
- 上传插件包（zip → 解压到 integrations/）
- 导出已有插件（打包 zip 下载）

## 2. 决策记录

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 下载含义 | 导出已有插件（非远程市场） | 无外部依赖，工作量可控 |
| 禁用状态存储 | config.json `integration.disabled_plugins: []` | 与现有 config 模式一致，重启不丢 |
| 上传信任模型 | 需认证 + manifest 校验 + 入口脚本存在性检查 | 子进程插件=任意代码执行，必须有限校验 |
| 上传格式 | zip 包，根目录含 manifest.json + entry 脚本 | 约定清晰，解压即用 |

## 3. 后端设计

### 3.1 禁用状态

`config.json` 加 `disabled_plugins` 数组：
```json
"integration": {
    "disabled_plugins": ["some_plugin"]
}
```
`manifest_loader` 跳过 disabled 列表里的插件（不加载、不启动进程）。

`config_helper` 加 `get_disabled_plugins()` / `set_plugin_disabled(id, disabled)`。

### 3.2 路由（`integration_routes.py` 扩展）

| 路由 | 方法 | 功能 |
|------|------|------|
| `/api/integrations` | GET | 列表 + 状态（已有，扩展含 enabled 字段） |
| `/api/integrations/{id}/toggle-enabled` | POST | 启用↔禁用切换 |
| `/api/integrations/{id}/export` | GET | 打包插件为 zip 下载 |
| `/api/integrations/upload` | POST | 上传 zip 包，校验+解压 |

### 3.3 上传安全校验

1. **认证**：需登录（已有 api_token_guard）
2. **格式校验**：zip 内必须有 manifest.json，且 `id` 合法（字母数字下划线，防路径穿越）
3. **入口校验**：manifest 的 `entry` 文件必须在 zip 内存在
4. **冲突检测**：同名插件已存在 → 拒绝（或覆盖需确认）
5. **解压**：到 `integrations/{id}/`，原子化（先解压临时目录再 rename）

⚠️ 不做：代码内容审计（子进程插件本质信任，审计成本过高）。安全靠"谁能上传"（认证）而非"上传什么"。

### 3.4 导出

`GET /api/integrations/{id}/export` → 把 `integrations/{id}/` 打包 zip，`StreamingResponse` 返回。

## 4. 前端设计

### 4.1 slash 命令

`ChatView.vue` 的 `SLASH_COMMANDS` 加：
```js
{ cmd: '/plugin', desc: '插件管理', action: 'nav', url: '/plugin' },
```

### 4.2 路由 + 视图

`router/index.js` 加 `/plugin` → `PluginManageView.vue`。

视图含：
- 插件列表卡片（id/name/状态/能力）
- 每个卡片：启用/禁用 toggle + 导出按钮 + 删除按钮（禁用态可删）
- 上传区：拖拽 zip 或点击选择

## 5. 实施步骤

| Step | 内容 |
|------|------|
| 1 | 后端：disabled_plugins config + loader 跳过 + toggle-enabled 路由 |
| 2 | 后端：export（zip 打包下载）+ upload（zip 校验解压）路由 |
| 3 | 前端：slash 命令 + 路由 + PluginManageView 视图 |
| 4 | 测试 + 文档 |
