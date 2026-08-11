/**
 * 欢迎语 composable（从 ChatView 拆出）。
 *
 * 进入主页时根据当前时段 + 房主名生成一句问候，淡入 1.5s 后淡出。
 * 自包含：管理自己的 showGreeting / greetingText / 定时器，
 * 组件卸载时由 onScopeDispose 清掉残留计时器。
 */
import { ref, onScopeDispose } from 'vue'

export function useGreeting() {
  const showGreeting = ref(false)
  const greetingText = ref('')
  let greetingTimer = null

  function getGreeting(ownerName) {
    const hour = new Date().getHours()
    let timeGreeting = ''

    if (hour >= 5 && hour < 12) {
      timeGreeting = '早上好'
    } else if (hour >= 12 && hour < 14) {
      timeGreeting = '中午好'
    } else if (hour >= 14 && hour < 18) {
      timeGreeting = '下午好'
    } else {
      timeGreeting = '晚上好'
    }

    return ownerName ? `${timeGreeting}，${ownerName}` : timeGreeting
  }

  async function showGreetingMessage() {
    try {
      const res = await fetch('/api/home/info')
      const json = await res.json()
      const ownerName = json.data?.owner_name || ''

      greetingText.value = getGreeting(ownerName)
      showGreeting.value = true

      // 1.5秒后开始淡化
      greetingTimer = setTimeout(() => {
        showGreeting.value = false
      }, 1500)
    } catch (e) {
      console.error('Failed to load home info for greeting:', e)
    }
  }

  // 组件卸载时清掉残留计时器，避免回调在卸载后触发。
  onScopeDispose(() => {
    if (greetingTimer) clearTimeout(greetingTimer)
  })

  return { showGreeting, greetingText, showGreetingMessage }
}
