/**
 * 轻量 API 工具函数 — 统一 fetch + json.data 解包模式
 */

/**
 * 解析响应：非 2xx 抛错（带后端 message），2xx 解包 json.data。
 *
 * 后端错误响应形如 ApiResponse(code, message, data=None)，
 * 旧实现直接返回 json.data ?? json 会把整个错误对象当成功数据返回。
 *
 * @param {Response} res
 * @returns {Promise<any>} 解包后的 data
 * @throws {Error} message 取自后端 json.message，无 JSON 时带 status；
 *   额外挂 err.status（HTTP 状态码），供调用方区分「资源已失效」等可恢复语义
 */
async function _unwrap(res) {
  let json = null
  // 502 等非 JSON 响应：res.json() 会抛，单独兜底
  try {
    json = await res.json()
  } catch {
    throw Object.assign(new Error(`请求失败：HTTP ${res.status}`), { status: res.status })
  }
  if (!res.ok) {
    throw Object.assign(
      new Error(json?.message || `请求失败：HTTP ${res.status}`),
      { status: res.status },
    )
  }
  return json.data ?? json
}

/**
 * GET 请求并自动解包 responseData。
 * 等价于: const res = await fetch(url); const json = await res.json(); return json.data ?? json
 * 非法状态码（4xx/5xx）抛错，由调用方 try/catch。
 *
 * @param {string} url - API 路径
 * @param {RequestInit} [options] - fetch 选项（默认 credentials: 'include'）
 * @returns {Promise<any>} 解包后的数据
 */
export async function apiGet(url, options = {}) {
  const res = await fetch(url, { credentials: 'include', ...options })
  return _unwrap(res)
}

/**
 * POST 请求并自动解包 responseData。
 * 非法状态码（4xx/5xx）抛错，由调用方 try/catch。
 *
 * @param {string} url - API 路径
 * @param {any} body - 请求体（自动 JSON 序列化）
 * @param {RequestInit} [options] - fetch 选项
 * @returns {Promise<any>} 解包后的数据
 */
export async function apiPost(url, body, options = {}) {
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    ...options,
  })
  return _unwrap(res)
}

/**
 * PUT 请求并自动解包 responseData。
 *
 * @param {string} url - API 路径
 * @param {any} body - 请求体（自动 JSON 序列化）
 * @param {RequestInit} [options] - fetch 选项
 * @returns {Promise<any>} 解包后的数据
 */
export async function apiPut(url, body, options = {}) {
  const res = await fetch(url, {
    method: 'PUT',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    ...options,
  })
  return _unwrap(res)
}

/**
 * DELETE 请求并自动解包 responseData。
 *
 * @param {string} url - API 路径
 * @param {RequestInit} [options] - fetch 选项
 * @returns {Promise<any>} 解包后的数据
 */
export async function apiDelete(url, options = {}) {
  const res = await fetch(url, { method: 'DELETE', credentials: 'include', ...options })
  return _unwrap(res)
}

/** 422 校验错误里常见字段的中文标签（loc 的末段） */
const _FIELD_LABELS = {
  username: '用户名',
  password: '密码',
  code: '邀请码',
  display_name: '显示名称',
  token: '令牌',
  api_key: 'API Key',
  base_url: 'API 地址',
  model: '模型名称',
  url: '地址',
  audio: '音频',
}

/**
 * 从后端错误响应 JSON 中提取可读的错误文案。
 *
 * 覆盖两种错误形态：
 * - AppException：{code, message, data} → 取 message
 * - FastAPI 422 参数校验：{detail: [{type, loc, msg, ctx}]} → 按字段名 + 类型转中文
 *   （直接 new Error(json.detail) 会把数组强转成 "[object Object]" 渲染到表单上）
 *
 * @param {any} json - 后端响应 JSON（可能为 null / 未知形状）
 * @param {string} [fallback] - 无法识别时的兜底文案
 * @returns {string} 可直接展示的错误文案
 */
export function extractApiError(json, fallback = '请求失败') {
  if (!json) return fallback
  if (typeof json.detail === 'string' && json.detail) return json.detail
  if (Array.isArray(json.detail) && json.detail.length > 0) {
    const first = json.detail[0]
    const label = _FIELD_LABELS[first?.loc?.at(-1)] || '输入'
    if (first?.type === 'string_too_short') {
      return `${label}长度不足（至少 ${first.ctx?.min_length ?? '若干'} 位）`
    }
    if (first?.type === 'string_too_long') {
      return `${label}过长（最多 ${first.ctx?.max_length ?? '若干'} 位）`
    }
    if (first?.type === 'missing') return `请填写${label}`
    if (first?.msg) return first.msg
  }
  if (typeof json.message === 'string' && json.message) return json.message
  return fallback
}

