import { describe, it, expect, beforeEach } from 'vitest'
import {
  chatSessionStorageKey,
  getChatSessionId,
  setChatSessionId,
  clearChatSession,
  resetUserScopedState,
} from '../../src/utils/storage'
import { SS_CHAT_SESSION } from '../../src/utils/constants'

describe('chatSessionStorageKey', () => {
  it('namespaces the chat session key by username', () => {
    expect(chatSessionStorageKey('alice')).toBe(`${SS_CHAT_SESSION}:alice`)
  })

  it('falls back to the global key when username is missing', () => {
    expect(chatSessionStorageKey(undefined)).toBe(SS_CHAT_SESSION)
    expect(chatSessionStorageKey('')).toBe(SS_CHAT_SESSION)
  })
})

describe('chat session accessors', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('writes and reads under the namespaced key', () => {
    setChatSessionId('alice', 'sess-1')
    // 隔离：写到 alice 的键，全局键与 bob 的键都不受影响
    expect(sessionStorage.getItem(`${SS_CHAT_SESSION}:alice`)).toBe('sess-1')
    expect(sessionStorage.getItem(SS_CHAT_SESSION)).toBeNull()
    expect(getChatSessionId('alice')).toBe('sess-1')
    expect(getChatSessionId('bob')).toBeNull()
  })

  it('isolates sessions per user', () => {
    setChatSessionId('alice', 'sess-alice')
    setChatSessionId('bob', 'sess-bob')
    expect(getChatSessionId('alice')).toBe('sess-alice')
    expect(getChatSessionId('bob')).toBe('sess-bob')
  })

  it('clearChatSession only removes the given user key', () => {
    setChatSessionId('alice', 'sess-alice')
    setChatSessionId('bob', 'sess-bob')
    clearChatSession('alice')
    expect(getChatSessionId('alice')).toBeNull()
    expect(getChatSessionId('bob')).toBe('sess-bob') // bob 不受影响
  })
})

describe('resetUserScopedState', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('clears the given user chat session', () => {
    setChatSessionId('alice', 'sess-alice')
    resetUserScopedState('alice')
    expect(getChatSessionId('alice')).toBeNull()
  })

  it('does not touch other users', () => {
    setChatSessionId('alice', 'sess-alice')
    setChatSessionId('bob', 'sess-bob')
    resetUserScopedState('alice')
    expect(getChatSessionId('bob')).toBe('sess-bob')
  })

  it('tolerates missing username (falls back to global key)', () => {
    setChatSessionId(undefined, 'sess-anon')
    resetUserScopedState(undefined)
    expect(getChatSessionId(undefined)).toBeNull()
  })
})
