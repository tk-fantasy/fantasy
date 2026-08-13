import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref } from 'vue'
import { useEntityMeta } from '../../src/composables/useEntityMeta'

describe('useEntityMeta operable', () => {
  beforeEach(() => {
    global.fetch = vi.fn()
  })

  it('loadEntityOperable 填充 entityOperable', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ data: { disabled: { 'lock.tong_suo': '0' } } }),
    })
    const { entityOperable, loadEntityOperable } = useEntityMeta(ref(null), ref(null))
    await loadEntityOperable()
    expect(entityOperable.value['lock.tong_suo']).toBe('0')
  })

  it('toggleOperable 禁用→发 PUT operable:false 并写入本地', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ data: {} }) })
    const { entityOperable, toggleOperable } = useEntityMeta(ref(null), ref(null))
    await toggleOperable('light.bed') // 当前允许 → 禁用
    expect(entityOperable.value['light.bed']).toBe('0')
    expect(global.fetch).toHaveBeenCalledWith('/api/ha/entity-operable', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ entity_id: 'light.bed', operable: false }),
    }))
  })

  it('toggleOperable 恢复→发 PUT operable:true 并删除本地', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ data: {} }) })
    const { entityOperable, toggleOperable } = useEntityMeta(ref(null), ref(null))
    entityOperable.value['lock.tong_suo'] = '0' // 预置为禁用
    await toggleOperable('lock.tong_suo') // 禁用 → 恢复
    expect(entityOperable.value['lock.tong_suo']).toBeUndefined()
    expect(global.fetch).toHaveBeenCalledWith('/api/ha/entity-operable', expect.objectContaining({
      body: JSON.stringify({ entity_id: 'lock.tong_suo', operable: true }),
    }))
  })
})
