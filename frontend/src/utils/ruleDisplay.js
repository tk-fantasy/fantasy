/**
 * 规则摘要的展示格式化 —— TaskView 与 ReviseChatModal 共用。
 *
 * 历史教训：这套函数曾以「复制保持自包含」的方式在两个 view 各养一份，
 * 各自独立修 bug 后双向漂移：条件渲染缺 visual/time/weather、直连格式
 * 动词表缺 set_temperature、null 条件一边渲染 "null" 一边渲染 "—"。
 * 现在收敛到这里，展示行为改动只改这一份。
 */

// MCP 工具格式与直连格式共用一张动词表（两种形状在 formatSingleAction 内归一）
const SERVICE_MAP = {
  turn_on: '打开',
  turn_off: '关闭',
  open_cover: '打开',
  close_cover: '关闭',
  set_temperature: '设置温度',
  set_brightness: '设置亮度',
}

export function formatCondition(condition) {
  if (!condition) return '—'
  if (typeof condition === 'string') return condition
  if (condition.description) return condition.description
  if (condition.type) return condition.type
  if (condition.visual) return `视觉: ${condition.visual}`
  if (condition.time) return `时间: ${condition.time}`
  if (condition.weather) return `天气: ${condition.weather}`
  return JSON.stringify(condition)
}

// 优先用 LLM 生成的中文描述(action_descriptions,如"关闭大门"),
// 缺了才从 actions 的 entity_id 解析。entity_id 常是机器拼音/ID 乱码,
// 无法还原"大门"这类可读名字,故描述字段优先。返回数组供 v-for 使用。
export function formatActions(actions, descriptions) {
  const descs = Array.isArray(descriptions) ? descriptions : []
  if (descs.length && !actions) return descs.slice()
  if (!actions) return []
  if (typeof actions === 'string') {
    if (descs[0]) return [descs[0]]
    try {
      const parsed = JSON.parse(actions)
      // 字符串可能装的是整个数组，递归回本函数逐项处理；单对象才直送
      if (Array.isArray(parsed)) return formatActions(parsed, descs)
      return [formatSingleAction(parsed)]
    } catch {
      return [actions]
    }
  }
  if (Array.isArray(actions)) {
    // 没有动作,但有描述也显示描述
    if (!actions.length) return descs.slice()
    return actions.map((a, idx) => {
      if (descs[idx]) return descs[idx]
      if (typeof a === 'string') {
        try {
          return formatSingleAction(JSON.parse(a))
        } catch {
          return a
        }
      }
      return formatSingleAction(a)
    })
  }
  // 单对象
  if (descs[0]) return [descs[0]]
  return [formatSingleAction(actions)]
}

export function formatSingleAction(action) {
  if (!action) return ''
  if (typeof action === 'string') return action
  const ti = action.mcp_tool_input || action
  const entity = ti.entity_id || ''
  const name = (entity.split('.')[1] || entity).replace(/_/g, ' ')
  const service = ti.service || ''
  return `${name} ${SERVICE_MAP[service] || service}`
}
