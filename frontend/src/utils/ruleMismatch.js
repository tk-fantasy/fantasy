/**
 * 规则 type ↔ 摄像头绑定错配检测（纯函数，TaskView 卡片标色用）。
 *
 * type 缺失/非法按 vision 兜底 —— 与后端 rule_service 的落库兜底语义一致
 * （automation_service 路由同样把未知 type 归入 vision 管道评估）。
 *
 * @param {object} rule 规则对象（含 type、camera_id 字段）
 * @returns {'red' | 'orange' | ''} red=全局视觉规则（危险）；orange=绑定摄像头的定时/天气规则（无害提示）
 */
export function getRuleMismatch(rule) {
  if (!rule) return ''
  // 缺失/非法 type 兜底为 vision：仅 time/weather 为合法非视觉类型，其余一律按 vision
  const raw = String(rule.type || '').toLowerCase()
  const type = raw === 'time' || raw === 'weather' ? raw : 'vision'
  const cam = rule.camera_id || ''
  if (type === 'vision' && !cam) return 'red'
  if ((type === 'time' || type === 'weather') && cam) return 'orange'
  return ''
}
