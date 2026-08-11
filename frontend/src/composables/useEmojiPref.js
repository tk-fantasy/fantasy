/**
 * Emoji 偏好管理 composable（审查 #15：去重）。
 *
 * 原 WeatherWidget/HAListView/ScheduledTasksView/TaskView 各自复制了一份
 * emojiPrefs + loadEmojiPrefs + openEmojiPicker + onEmojiSelect，逻辑完全
 * 一致。此处统一封装，调用方只 destructure 需要的部分。
 *
 * 偏好键格式：`${scope}:${key}`，如 `weather:100`、`entity:light.xxx`。
 */
import { ref } from 'vue'

export function useEmojiPref() {
  /** 偏好字典 { "scope:key": "emoji" } */
  const emojiPrefs = ref({})
  /** EmojiPicker 显隐 */
  const showEmojiPicker = ref(false)
  /** 当前选中的目标 { scope, key }，供 onEmojiSelect 回写 */
  const currentEmojiTarget = ref(null)

  /** 拉取全部偏好，转成 { scope:key: emoji } 字典 */
  async function loadEmojiPrefs() {
    try {
      const res = await fetch('/api/emoji/preferences')
      if (!res.ok) return
      const json = await res.json()
      const prefs = {}
      for (const item of (json.data || [])) {
        prefs[`${item.scope}:${item.key}`] = item.emoji_char
      }
      emojiPrefs.value = prefs
    } catch (e) {
      console.error('Failed to load emoji prefs:', e)
    }
  }

  /** 打开 EmojiPicker，记下当前目标（scope/key）供 select 回写 */
  function openEmojiPicker(scope, key) {
    currentEmojiTarget.value = { scope, key }
    showEmojiPicker.value = true
  }

  /** EmojiPicker 选中后回写：PUT 到后端 + 更新本地字典 */
  async function onEmojiSelect(item) {
    if (!currentEmojiTarget.value) return
    const { scope, key } = currentEmojiTarget.value
    try {
      await fetch('/api/emoji/preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope, key, emoji_char: item.char }),
      })
      emojiPrefs.value[`${scope}:${key}`] = item.char
    } catch (e) {
      console.error('Failed to save emoji pref:', e)
    }
  }

  /** 读偏好，未配置回退到 defaultEmoji */
  function getEmoji(scope, key, defaultEmoji = '') {
    return emojiPrefs.value[`${scope}:${key}`] || defaultEmoji
  }

  return {
    emojiPrefs,
    showEmojiPicker,
    currentEmojiTarget,
    loadEmojiPrefs,
    openEmojiPicker,
    onEmojiSelect,
    getEmoji,
  }
}
