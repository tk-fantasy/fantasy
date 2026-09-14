import { describe, it, expect } from 'vitest'
import {
  buildInviteRegisterLink,
  isLoopbackHost,
  inviteRemainMs,
  formatRemainMs
} from '../../src/utils/inviteLink'

describe('buildInviteRegisterLink', () => {
  it('拼出带 origin 与码的注册深链', () => {
    expect(buildInviteRegisterLink('http://192.168.1.5:8010', 'AB3D-EF7H'))
      .toBe('http://192.168.1.5:8010/login?mode=register&code=AB3D-EF7H')
  })
  it('去掉末尾斜杠并编码特殊字符', () => {
    expect(buildInviteRegisterLink('http://host:8010/', 'A/B C'))
      .toBe('http://host:8010/login?mode=register&code=A%2FB%20C')
  })
})

describe('isLoopbackHost', () => {
  it('识别 localhost/127.0.0.1，放过局域网 IP', () => {
    expect(isLoopbackHost('localhost')).toBe(true)
    expect(isLoopbackHost('127.0.0.1')).toBe(true)
    expect(isLoopbackHost('192.168.1.5')).toBe(false)
    expect(isLoopbackHost('')).toBe(false)
  })
})

describe('inviteRemainMs / formatRemainMs', () => {
  it('无 expires_at（存量码）返回 null', () => {
    expect(inviteRemainMs({})).toBeNull()
    expect(inviteRemainMs({ expires_at: 0 })).toBeNull()
  })
  it('剩余毫秒与格式化', () => {
    const now = Date.now()
    expect(inviteRemainMs({ expires_at: now - 1 })).toBeLessThanOrEqual(0)
    expect(formatRemainMs(30 * 60000)).toBe('30 分钟')
    expect(formatRemainMs(25 * 3600000)).toBe('25 小时')
    expect(formatRemainMs(-1)).toBe('已过期')
    expect(formatRemainMs(null)).toBe('')
  })
})
