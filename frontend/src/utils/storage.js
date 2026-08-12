/**
 * 用户命名空间的存储访问 + 身份切换时的 user-scoped 状态清理。
 *
 * 背景：此前聊天会话 ID 用全局 sessionStorage 键（aether_chat_session_id），切换/登出
 * 时靠手动 removeItem 防串号。改造为按用户命名空间后，各用户天然隔离；本模块是唯一的
 * 存储访问收口，未来 user-scoped key 在此追加，调用方无需感知 key 拼接细节。
 *
 * 注意：这里只管"存储层"。各 composable/view 持有的 in-memory 状态仍需靠
 * window.location.reload() 重置；去除 reload 需引入中央 store（pinia），属后续工作。
 */
import { SS_CHAT_SESSION } from './constants'

/**
 * 按用户命名空间的聊天会话存储键：`aether_chat_session_id:<username>`。
 * 无 username（登录前/异常）时退回全局键兜底，避免写入意外路径。
 * @param {string|undefined} username
 * @returns {string}
 */
export function chatSessionStorageKey(username) {
  return username ? `${SS_CHAT_SESSION}:${username}` : SS_CHAT_SESSION
}

export function getChatSessionId(username) {
  return sessionStorage.getItem(chatSessionStorageKey(username))
}

export function setChatSessionId(username, id) {
  sessionStorage.setItem(chatSessionStorageKey(username), id)
}

export function clearChatSession(username) {
  sessionStorage.removeItem(chatSessionStorageKey(username))
}

/**
 * 清理某用户的全部 user-scoped 存储状态。
 * 登出/会话过期时调用（切换用户因命名空间天然隔离，无需调用）。
 * 当前只清聊天会话；新增 user-scoped key 时在此追加 —— 单一扩展点。
 * @param {string|undefined} username
 */
export function resetUserScopedState(username) {
  clearChatSession(username)
}
