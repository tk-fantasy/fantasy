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
 * @throws {Error} message 取自后端 json.message，无 JSON 时带 status
 */
async function _unwrap(res) {
  let json = null
  // 502 等非 JSON 响应：res.json() 会抛，单独兜底
  try {
    json = await res.json()
  } catch {
    throw new Error(`请求失败：HTTP ${res.status}`)
  }
  if (!res.ok) {
    throw new Error(json?.message || `请求失败：HTTP ${res.status}`)
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
