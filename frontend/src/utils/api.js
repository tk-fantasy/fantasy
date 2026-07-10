/**
 * 轻量 API 工具函数 — 统一 fetch + json.data 解包模式
 */

/**
 * GET 请求并自动解包 responseData。
 * 等价于: const res = await fetch(url); const json = await res.json(); return json.data ?? json
 *
 * @param {string} url - API 路径
 * @param {RequestInit} [options] - fetch 选项（默认 credentials: 'include'）
 * @returns {Promise<any>} 解包后的数据
 */
export async function apiGet(url, options = {}) {
  const res = await fetch(url, { credentials: 'include', ...options })
  const json = await res.json()
  return json.data ?? json
}

/**
 * POST 请求并自动解包 responseData。
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
  const json = await res.json()
  return json.data ?? json
}
