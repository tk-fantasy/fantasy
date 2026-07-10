/**
 * 工具调用友好化显示 — 工具元数据 + 摘要格式化函数。
 *
 * 后端 tool_response.result 是 JSON 编码的字符串（langchain_tools._coroutine 做 json.dumps），
 * 前端需 JSON.parse 才能拿到 handler 原始返回的 dict。
 * 失败路径（handler 返回含 "error" key 的 dict）走 error_message，不带 tool_response。
 */

// 工具名 → 图标 + 中文标签
export const TOOL_META = {
  call_service:     { icon: '🔧', label: '控制设备' },
  get_entities:     { icon: '📋', label: '查询设备' },
  web_search:       { icon: '🔍', label: '搜索' },
  fetch_webpage:    { icon: '🌐', label: '抓取网页' },
  vision_chat:      { icon: '👁', label: '查看画面' },
  describe_state:   { icon: '📸', label: '查询状态' },
  verify_condition: { icon: '🔎', label: '验证条件' },
  verify_action:    { icon: '✓',  label: '确认操作' },
}

// HA service 名中文化（call_service 的 service 参数）
const SERVICE_LABELS = {
  turn_on: '开启', turn_off: '关闭', toggle: '切换',
  set_temperature: '设置温度', set_hvac_mode: '设置模式', set_fan_mode: '设置风速',
  open_cover: '打开', close_cover: '关闭', stop_cover: '停止', set_cover_position: '设置位置',
  set_position: '设置位置', select_option: '选择', select_source: '选择源',
  set_brightness: '设置亮度', set_color: '设置颜色', set_percentage: '设置百分比',
  play_media: '播放', media_play: '播放', media_pause: '暂停', media_stop: '停止',
  media_next: '下一曲', media_previous: '上一曲', volume_set: '设置音量',
  vacuum_start: '开始清扫', vacuum_pause: '暂停', vacuum_return_to_base: '回充', vacuum_stop: '停止',
  lock: '上锁', unlock: '解锁',
}

/**
 * 从完整工具名中提取短名（去掉 client_id___ 前缀）。
 * 后端 tool_name 可能是 "ha_devices___call_service" 或 "local___vision_chat"。
 */
export function shortToolName(toolName) {
  if (!toolName) return ''
  if (toolName.includes('___')) return toolName.split('___')[1]
  return toolName
}

/**
 * 获取工具的图标，未知工具返回默认齿轮。
 */
export function toolIcon(toolName) {
  const name = shortToolName(toolName)
  return TOOL_META[name]?.icon || '⚙'
}

/**
 * 解析 tool_response.result（JSON 字符串 → dict），容错。
 * 成功路径：response = { result: "<json string>" }
 * 失败路径：response = null（错误信息在 error 字段）
 */
export function parseToolResult(response) {
  if (!response?.result) return null
  try {
    return JSON.parse(response.result)
  } catch {
    return null
  }
}

/**
 * 生成调用摘要（单行人话）—— 用户看到的第一行。
 * @param toolName 完整工具名
 * @param params tool_params dict
 * @param friendlyName 后端填充的设备友好名（仅 call_service）
 */
export function summarizeToolCall(toolName, params, friendlyName) {
  const name = shortToolName(toolName)
  const p = params || {}

  switch (name) {
    case 'call_service': {
      const service = p.service || ''
      const serviceLabel = SERVICE_LABELS[service] || service || '操作'
      const target = friendlyName || p.entity_id || '设备'
      return `${serviceLabel} ${target}`
    }
    case 'get_entities':
      return '查询设备列表'
    case 'web_search':
      return `搜索: ${p.query || ''}`
    case 'fetch_webpage': {
      const url = p.url || ''
      try { return `抓取: ${new URL(url).hostname}` } catch { return `抓取: ${url}` }
    }
    case 'vision_chat':
      return `查看画面${p.question ? ': ' + truncate(p.question, 30) : ''}`
    case 'describe_state':
      return '查询当前状态'
    case 'verify_condition':
      return `验证条件${p.condition ? ': ' + truncate(p.condition, 30) : ''}`
    case 'verify_action': {
      const service = p.service || ''
      const serviceLabel = SERVICE_LABELS[service] || service || '操作'
      const target = p.entity_id || '设备'
      return `确认${serviceLabel} ${target}`
    }
    default:
      return name || '调用工具'
  }
}

/**
 * 生成结果摘要（工具执行完成后的简短状态）。
 * @param toolName 完整工具名
 * @param success 是否成功
 * @param response tool_response dict（成功路径）
 * @param error error_message 字符串（失败路径）
 */
export function summarizeToolResult(toolName, success, response, error) {
  if (!success) {
    return summarizeError(toolName, error)
  }
  const name = shortToolName(toolName)
  const data = parseToolResult(response)

  switch (name) {
    case 'call_service': {
      if (!data) return '已执行'
      const newState = data.new_state
      if (newState && newState.state) return `已生效，当前: ${newState.state}`
      return '已执行'
    }
    case 'get_entities':
      return data?.count != null ? `共 ${data.count} 个设备` : '已获取'
    case 'web_search':
      if (!data) return '已完成'
      if (data.text) return '已获取搜索结果'
      if (data.results?.length === 0) return '未找到结果'
      return '已获取搜索结果'
    case 'fetch_webpage': {
      if (!data) return '已抓取'
      const parts = []
      if (data.title) parts.push(data.title)
      if (data.length) parts.push(`${data.length} 字`)
      return parts.join(' · ') || `HTTP ${data.status_code || ''}`
    }
    case 'vision_chat':
      if (!data) return '已完成'
      if (data.has_frame === false) return '摄像头无画面'
      return data.answer ? truncate(data.answer, 40) : '已分析'
    case 'describe_state':
      return '已获取'
    case 'verify_condition':
      // condition_met 总是 None（LLM 判断），这里只说数据已获取
      if (!data) return '已获取'
      return `已获取${data.type || ''}数据`
    case 'verify_action': {
      if (!data) return '已查询'
      if (data.verified) return '已确认生效'
      // verify_action 失败时走 error 路径，不会到这里
      return `当前: ${data.current_state || '未知'}`
    }
    default:
      return '已完成'
  }
}

/**
 * 生成错误摘要（失败路径）。
 * verify_action 的验证失败是「正常」失败（有 checks 数组），要友好显示。
 * error_message 格式："Error: <msg>\n原始返回：<json>"，需提取原始返回里的 checks。
 */
function summarizeError(toolName, error) {
  const name = shortToolName(toolName)

  // 尝试从 error_message 提取「原始返回：」后的 JSON（_coroutine 的错误包装格式）
  let data = null
  if (error) {
    const marker = '原始返回：'
    const idx = error.indexOf(marker)
    if (idx !== -1) {
      try { data = JSON.parse(error.slice(idx + marker.length)) } catch { /* 忽略 */ }
    }
  }

  if (name === 'verify_action' && data?.checks) {
    const failed = data.checks.filter(c => !c.passed)
    if (failed.length) {
      const detail = failed.map(c => `${c.attribute} 期望${c.expected}实际${c.actual}`).join(', ')
      return `未生效: ${detail}`
    }
  }

  // 提取 "Error: xxx" 部分的简短信息
  if (error) {
    const match = error.match(/^Error:\s*(.+?)(\n|$)/)
    if (match) return truncate(match[1], 60)
    return truncate(error, 60)
  }
  return '执行失败'
}

/**
 * 截断字符串，超出部分用 ... 替代。
 */
function truncate(str, maxLen) {
  if (!str) return ''
  if (str.length <= maxLen) return str
  return str.slice(0, maxLen) + '...'
}
