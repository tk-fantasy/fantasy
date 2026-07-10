/**
 * 从 HA entity 的 attributes + services 数据动态推导控件列表
 * 零硬编码 — 全部从 HA API 数据中推导
 */

/**
 * 从 entity 推导出控件列表
 * @param {Object} entity - HA entity 对象
 * @param {Object} services - HA 服务定义 {domain: {service_name: {fields: [], required: []}}}
 * @returns {Array} 控件列表
 */
export function resolveCapabilities(entity, services) {
  const capabilities = []
  const attrs = entity.attributes || {}
  const domain = entity.entity_id.split('.')[0]
  const domainServices = services?.[domain] || {}
  const processedBases = new Set()
  const handledParams = new Set()

  // 收集所有属性名（用于 action 的概念匹配）
  const attrNames = new Set(Object.keys(attrs))
  // 从属性名中提取词根（下划线分词）
  for (const name of attrNames) {
    for (const word of name.split('_')) {
      if (word.length >= 2) attrNames.add(word)
    }
  }

  // 1. 推导 enum 控件（数组属性）
  for (const [attrName, attrValue] of Object.entries(attrs)) {
    if (!Array.isArray(attrValue) || attrValue.length < 2) continue

    const targetField = attrName.endsWith('s') ? attrName.slice(0, -1) : attrName

    const match = findServiceForField(domainServices, domain, targetField, attrNames)
    if (!match) continue

    let current
    let currentAttr
    if (attrName.endsWith('s')) {
      const baseName = attrName.slice(0, -1)
      current = attrs[baseName] ?? entity.state
      currentAttr = baseName
      processedBases.add(baseName)
    } else {
      current = attrValue
      currentAttr = attrName
    }

    capabilities.push({
      key: attrName,
      label: formatLabel(attrName),
      type: 'enum',
      options: attrValue,
      current: current,
      currentAttr: currentAttr,
      service: match.domain,
      action: match.serviceName,
      param: match.field,
    })
    handledParams.add(match.field)
  }

  // 1b. 单数属性 → enum（如 fan_mode 借助 fan_modes 数组生成控件）
  for (const [attrName, attrValue] of Object.entries(attrs)) {
    if (Array.isArray(attrValue)) continue
    if (typeof attrValue !== 'string') continue
    if (processedBases.has(attrName)) continue

    const pluralKey = attrName + 's'
    const options = attrs[pluralKey]
    if (!Array.isArray(options) || options.length < 2) continue

    const match = findServiceForField(domainServices, domain, attrName, attrNames)
    if (!match) continue

    capabilities.push({
      key: pluralKey,
      label: formatLabel(attrName),
      type: 'enum',
      options: options,
      current: attrValue,
      currentAttr: attrName,
      service: match.domain,
      action: match.serviceName,
      param: match.field,
    })
    handledParams.add(match.field)
  }

  // 2. 推导 slider 控件（数值属性）
  for (const [attrName, attrValue] of Object.entries(attrs)) {
    if (attrName.startsWith('current_') && ['sensor', 'binary_sensor'].includes(domain)) continue
    if (attrValue !== null && typeof attrValue !== 'number') continue
    if (Array.isArray(attrValue)) continue
    if (attrName.startsWith('supported_')) continue
    if (/^(min|max)_/.test(attrName)) continue
    if (attrName.endsWith('_step')) continue

    const targetField = attrName

    // 动态：如果属性名是 current_X 且直接匹配失败，自动去前缀重试 → position, tilt_position 等
    let match = findServiceForFieldPctPreferred(domainServices, domain, attrName)
    if (!match && attrName.startsWith('current_')) {
      match = findServiceForField(domainServices, domain, attrName.slice(8), attrNames)
    }
    if (!match) continue

    // 亮度类属性只发百分比，原始 brightness (0-255) 直接跳过
    if (match.field === 'brightness' && match.field === attrName) continue

    const isPctMatch = match.field === attrName + '_pct'

    let min = 0, max = 100, step = 1
    const minKey = findAttrKey(attrs, 'min', targetField)
    const maxKey = findAttrKey(attrs, 'max', targetField)
    const stepKey = findAttrKey(attrs, '', targetField, '_step')
    if (minKey !== null) min = attrs[minKey]
    if (maxKey !== null) max = attrs[maxKey]
    if (stepKey !== null) step = attrs[stepKey]

    let current = attrValue ?? min
    if (isPctMatch) {
      current = Math.round(current * (100 / 255))
      min = 0
      max = 100
    }

    capabilities.push({
      key: attrName,
      label: formatLabel(attrName),
      type: 'slider',
      min, max, step,
      current,
      unit: inferUnit(attrName, attrs),
      service: match.domain,
      action: match.serviceName,
      param: match.field,
      pctMatch: isPctMatch,
    })
    handledParams.add(match.field)
  }

  // 3. 推导 action 控件
  // 动态：跳过 turn_on/turn_off/toggle，跳过所有 set_/select_ 开头的参数驱动 service
  const actionList = []
  for (const [svcName, svcDef] of Object.entries(domainServices)) {
    if (svcName === 'turn_on' || svcName === 'turn_off' || svcName === 'toggle') continue
    if (svcName.startsWith('set_') || svcName.startsWith('select_')) continue

    const requiredFields = (svcDef.required || []).filter(f => f !== 'entity_id')
    if (requiredFields.length > 0) continue

    const allFields = svcDef.fields || []
    const nonEntityFields = allFields.filter(f => f !== 'entity_id')
    if (nonEntityFields.length < 1) continue
    if (nonEntityFields.every(f => handledParams.has(f))) continue

    // 动态概念匹配：service 名中的词是否与实体属性相关
    const words = svcName.split('_')
    const related = words.some(w =>
      w === domain || attrNames.has(w)
    )
    if (!related) continue

    actionList.push({
      label: formatServiceName(svcName, domain),
      service: domain,
      action: svcName,
      // 尝试找到对应的属性名（用于判断 active 状态）
      attrKey: words.find(w => attrs[w] !== undefined || attrs[w + 'ing'] !== undefined)
        ? (words.find(w => attrs[w] !== undefined) || words.find(w => attrs[w + 'ing'] !== undefined) + 'ing')
        : null,
    })
  }
  if (actionList.length > 0) {
    capabilities.push({
      key: '_actions',
      label: 'Actions',
      type: 'action',
      actions: actionList,
    })
  }

  return capabilities
}

/**
 * 查找 min/max/step 属性键，支持缩写匹配
 * 动态：prefix_targetField_suffix → min_temperature, max_temp, _step
 */
function findAttrKey(attrs, prefix, targetField, suffix = '') {
  // 精确：min_temperature
  const exactKey = `${prefix}_${targetField}${suffix}`
  if (attrs[exactKey] !== undefined) return exactKey
  // 保留兼容：mintemperature
  const noSep = `${prefix}${targetField}${suffix}`
  if (attrs[noSep] !== undefined) return noSep
  // 缩写：min_temp  → base=temp, temperature 以 temp 开头
  for (const key of Object.keys(attrs)) {
    if (!key.startsWith(prefix) || !key.endsWith(suffix)) continue
    const afterPrefix = key.slice(prefix.length, key.length - suffix.length)
    const base = afterPrefix.startsWith('_') ? afterPrefix.slice(1) : afterPrefix
    if (base === targetField) continue
    if (targetField.startsWith(base) && base.length >= 3) return key
  }
  return null
}

/**
 * 亮度类属性优先匹配 _pct 字段，否则回退精确匹配
 */
function findServiceForFieldPctPreferred(domainServices, domain, fieldName) {
  const pctField = fieldName + '_pct'
  for (const [svcName, svcDef] of Object.entries(domainServices)) {
    const fields = svcDef.fields || []
    if (fields.includes(pctField)) {
      return { domain, serviceName: svcName, field: pctField }
    }
  }
  return findServiceForField(domainServices, domain, fieldName, null)
}

/**
 * 在 domain 的 services 中找到包含指定 field 的 service
 * 动态：精确 → _pct 后缀 → 概念匹配（field 名与属性名有交集）
 */
function findServiceForField(domainServices, domain, fieldName, attrNames) {
  for (const [svcName, svcDef] of Object.entries(domainServices)) {
    const fields = svcDef.fields || []
    if (fields.includes(fieldName)) {
      return { domain, serviceName: svcName, field: fieldName }
    }
  }
  // _pct 后缀回退：brightness → brightness_pct
  const pctField = fieldName + '_pct'
  for (const [svcName, svcDef] of Object.entries(domainServices)) {
    const fields = svcDef.fields || []
    if (fields.includes(pctField)) {
      return { domain, serviceName: svcName, field: pctField }
    }
  }
  // 概念匹配：service 的字段名与属性名集合有交集
  if (attrNames && attrNames.size > 0) {
    for (const [svcName, svcDef] of Object.entries(domainServices)) {
      const fields = svcDef.fields || []
      for (const f of fields) {
        const fParts = f.split('_')
        if (fParts.some(p => attrNames.has(p) && p.length >= 3)) {
          return { domain, serviceName: svcName, field: f }
        }
      }
    }
  }
  return null
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

function inferUnit(attrName, attrs) {
  return attrs.unit_of_measurement || ''
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
