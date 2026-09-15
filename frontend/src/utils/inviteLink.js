// 邀请码二维码深链与过期展示的纯函数（便于单测）

export function buildInviteRegisterLink(origin, code) {
  const base = (origin || '').replace(/\/+$/, '')
  return `${base}/login?mode=register&code=${encodeURIComponent(code)}`
}

export function isLoopbackHost(hostname) {
  const h = (hostname || '').toLowerCase()
  return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]'
}

// 存量码无 expires_at → null（视为永不过期）
export function inviteRemainMs(invite) {
  if (!invite || !invite.expires_at) return null
  return invite.expires_at - Date.now()
}

// 记录是否已终结（已使用/已吊销/已过期）——终结记录才允许删除清理
export function inviteIsDead(invite) {
  if (!invite) return false
  if (invite.used_at || invite.revoked_at) return true
  const remain = inviteRemainMs(invite)
  return remain !== null && remain <= 0
}

export function formatRemainMs(ms) {
  if (ms == null) return ''
  if (ms <= 0) return '已过期'
  const totalMin = Math.ceil(ms / 60000)
  return totalMin < 60 ? `${totalMin} 分钟` : `${Math.ceil(totalMin / 60)} 小时`
}
