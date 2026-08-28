# 摄像头预览弹窗：固定下拉切换 + 小屏滚动适配 设计

日期：2026-08-28
状态：已确认（切换器固定下拉、小屏适配方案 A 均经用户确认）

## 背景

ChatView 摄像头预览弹窗（`.camera-modal`）当前存在两个体验问题：

1. 弹窗内多路切换器（`CameraSwitcher.vue`）≤4 路渲染标签按钮一行排开、>4 路才退化为
   FlowSelect 下拉。标签在窄屏/多路时会换行，把弹窗撑高、高度不稳定。
2. 弹窗 `max-height: 90vh` + `overflow: hidden`，画面区 `.camera-stage` 固定
   `min-height: 300px`。短视口下弹窗内容总高超出 90vh 时，下方面板（PTZ/统计/识别
   反馈/提示/插件面板）被直接裁掉且无法滚动到达。历史上 `<img>` 固有尺寸曾溢出画面区
   盖住上方切换器（ChatView.vue 内 `.camera-feed` 注释），现已用
   `height:100% + object-fit: contain` 约束，需保持该约束不被破坏。

## 目标

1. 弹窗内摄像头切换器固定使用下拉列表（FlowSelect），弹窗高度恒定，不受路数/宽度影响。
2. 任意视口（含矮屏/窄屏）下：视频画面只在画面区内缩放，不遮挡其他内容；所有面板均可
   滚动到达，信息不丢失。

## 非目标

- 不改弹窗整体形态（保留居中 modal + 遮罩，用户已确认不做下拉式 popover）。
- 不动 `useCameraPreview` 状态机、`usePtz`、后端接口与插件挂载机制。

## 方案

### 1. CameraSwitcher 固定下拉

- `frontend/src/components/CameraSwitcher.vue`：删除标签分支（`MAX_TABS`、`useDropdown`、
  `.camera-tab` 及相关样式），`cameras.length > 1` 时恒渲染 FlowSelect；单路仍不渲染
  切换器。组件对外 props/events（`cameras` / `modelValue` / `change`）不变，
  ChatView 引用无需调整。
- `ChatView.vue` 弹窗底部提示文案「切换上方的标签或下拉列表」改为只提下拉列表。

### 2. 弹窗「固定头部 + 滚动主体」（方案 A）

- 模板结构调整：`.camera-modal-header` 与 `CameraSwitcher` 留在滚动区外；`.camera-stage`、
  PTZ 面板、`.camera-stats`、`.camera-feedback`、`.camera-hint`、`PluginSlot` 包进新增的
  `.camera-modal-body`。
- 新增样式：`.camera-modal-body { overflow-y: auto; min-height: 0; }`
  （`min-height: 0` 允许 flex 子项收缩，否则滚动不生效）。
- `.camera-modal` 保持 `max-height: 90vh; overflow: hidden`（hidden 仅用于圆角裁切，
  滚动交给 body）与 flex column。
- `.camera-stage` 的 `min-height` 由固定 `300px` 改为 `clamp(140px, 32vh, 300px)`，
  矮屏时画面区随视口收缩。
- `.camera-feed` 保持 `width:100%; height:100%; max-height:400px; object-fit:contain`
  不变——这是画面不溢出、不遮挡切换器的关键约束。

## 边界与已知取舍

- FlowSelect 下拉面板为绝对定位（z-index 100），切换器在滚动主体之外，下拉展开叠在主体
  上方，不受 body 滚动裁切。极端矮窗（弹窗可视高 < 约 360px）时下拉列表可能被
  `.camera-modal` 的 `overflow: hidden` 裁切——记录为已知边界，本次不处理。
- 主体出现滚动条属预期行为（矮屏下优先保证可达性）。

## 测试

- `frontend/tests/components/CameraSwitcher.test.js` 重写为纯下拉行为：≥2 路恒渲染
  FlowSelect（触发器展示当前路 label、选中新路 emit `change` 并收起、重选当前路不 emit）；
  单路不渲染切换器。
- `ChatView.test.js` 无 camera 相关断言，无需改动。
- 浏览器手动验证：宽屏（~1280×800）、矮屏（~900×600、~700×450）下打开弹窗：
  切换器恒为下拉、画面不出画面区、下方内容可滚动到达。
- 回归：`npm run test`（vitest）全量通过；`npm run build` 构建并同步到后端静态目录。
