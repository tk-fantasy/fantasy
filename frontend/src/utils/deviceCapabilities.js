/**
 * 设备控件适配器 — 把后端 _controls dict 转成前端模板期望的 array 格式
 *
 * 后端 entity_controls.py 的 resolve_controls() 返回 dict[str, dict]：
 *   { "fan_mode": {type:"enum", service:"set_fan_mode", param:"fan_mode", options:[...], current:"auto"},
 *     "brightness_pct": {type:"slider", service:"turn_on", param:"brightness_pct", min:0, max:100, step:1, current:50, unit:""},
 *     "open_cover": {type:"action", service:"open_cover", param:null} }
 *
 * 前端模板期望 array，每个元素有 key/label/type + 各类型专属字段，
 * service 是 domain（如 "climate"），action 是 service_name（如 "set_temperature"）。
 */

/**
 * 把后端 _controls dict 适配为前端控件数组
 * @param {Object} controls - 后端 resolve_controls 返回的 dict
 * @param {Object} entity - HA entity 对象（用于取 domain 和 attributes）
 * @returns {Array} 前端模板期望的控件数组
 */
export function adaptControls(controls, entity) {
  if (!controls || typeof controls !== 'object') return []
  const attrs = entity?.attributes || {}
  const domain = entity?.entity_id?.split('.')[0] || ''

  const result = []
  const actionList = []

  for (const [key, ctrl] of Object.entries(controls)) {
    if (!ctrl || !ctrl.type) continue

    // 后端 service �� HA service 名（如 set_temperature），拆成 domain + action
    const serviceName = ctrl.service || ''
    const param = ctrl.param

    if (ctrl.type === 'enum') {
      // currentAttr：单数属性名（key 去掉复数 s，或就是 key 本身）
      const currentAttr = key.endsWith('s') ? key.slice(0, -1) : key
      result.push({
        key,
        label: formatLabel(key),
        type: 'enum',
        options: ctrl.options || [],
        current: ctrl.current,
        currentAttr,
        service: domain,
        action: serviceName,
        param,
      })
    } else if (ctrl.type === 'slider') {
      const pctMatch = param != null && param.endsWith('_pct')
      result.push({
        key,
        label: formatLabel(key),
        type: 'slider',
        min: ctrl.min ?? 0,
        max: ctrl.max ?? 100,
        step: ctrl.step ?? 1,
        current: ctrl.current,
        unit: ctrl.unit || '',
        service: domain,
        action: serviceName,
        param,
        pctMatch,
      })
    } else if (ctrl.type === 'action') {
      // action 类型聚合为单个条目（与原 resolveCapabilities 输出一致）
      actionList.push({
        label: formatServiceName(serviceName, domain),
        service: domain,
        action: serviceName,
        attrKey: null,
      })
    }
  }

  if (actionList.length > 0) {
    result.push({
      key: '_actions',
      label: 'Actions',
      type: 'action',
      actions: actionList,
    })
  }

  return result
}

function formatLabel(attrName) {
  return attrName
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())
}

function formatServiceName(svcName, domain) {
  const words = svcName.split('_').filter(w => w !== domain)
  return words
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

export function formatSliderValue(cap) {
  const val = cap.current
  if (val == null || isNaN(val)) return '—'
  return Math.round(val) + (cap.unit || '')
}

export function toActualValue(cap, inputValue) {
  if (cap.inputScale) {
    return inputValue * cap.inputScale
  }
  return inputValue
}
