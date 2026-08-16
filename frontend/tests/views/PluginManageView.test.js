import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import PluginManageView from '../../src/views/PluginManageView.vue'

// 两插件：飞书带 secret 配置、xiaoai 带 enum 配置（对应 /api/integrations）
const PLUGINS = {
  plugins: [
    {
      id: 'feishu', name: '飞书机器人', version: '1.0.1',
      description: '飞书机器人', capabilities: ['host_integration'],
      alive: true, enabled: true, config_schema: {
        app_id: { type: 'string', required: true, label: 'App ID' },
        app_secret: { type: 'secret', required: true, label: 'App Secret' },
      }, has_config_set: true,
    },
    {
      id: 'xiaoai', name: '小爱音箱', version: '1.0.0',
      description: '', capabilities: [], alive: true, enabled: true,
      config_schema: {}, has_config_set: false,
    },
  ],
  enabled: true,
}

const CONFIG = {
  id: 'feishu',
  schema: PLUGINS.plugins[0].config_schema,
  values: { app_id: 'cli_x', app_secret: { is_set: true, masked: 'DsFx…gSOy' } },
  has_config_set: true,
}

global.fetch = vi.fn((url, opts) => {
  let body = { success: true, data: null }
  if (String(url).includes('/api/integrations/feishu/config')) {
    body.data = CONFIG
    if (opts?.method === 'POST') body.data = { id: 'feishu', applied: 'restarted' }
  } else if (String(url).includes('/api/integrations')) {
    body.data = PLUGINS
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
})

describe('PluginManageView 详情/配置弹窗', () => {
  beforeEach(() => vi.clearAllMocks())

  it('复用全局 AdvancedModal 外壳（modal-overlay/modal-container + Teleport）', async () => {
    const wrapper = mount(PluginManageView, { attachTo: document.body })
    await flushPromises()

    // 打开飞书详情
    const buttons = wrapper.findAll('button').filter(b => b.text() === '详情')
    expect(buttons.length).toBe(2)
    await buttons[0].trigger('click')
    await flushPromises()

    // AdvancedModal 渲染到 body（Teleport），带全局外壳类名
    const overlay = document.querySelector('.modal-overlay')
    expect(overlay).toBeTruthy()
    expect(document.querySelector('.modal-container')).toBeTruthy()
    expect(document.querySelector('.modal-header h2').textContent).toContain('飞书机器人')

    wrapper.unmount()
  })

  it('配置表单走 setting-row/setting-label/setting-input 全局样式体系', async () => {
    const wrapper = mount(PluginManageView, { attachTo: document.body })
    await flushPromises()
    await wrapper.findAll('button').filter(b => b.text() === '详情')[0].trigger('click')
    await flushPromises()

    const rows = document.querySelectorAll('.modal-container .setting-row')
    expect(rows.length).toBe(2)  // app_id + app_secret

    const label = rows[0].querySelector('.setting-label .label-text')
    expect(label.textContent).toContain('App ID')
    expect(rows[0].querySelector('.setting-input')).toBeTruthy()
    expect(label.querySelector('.required-mark').textContent).toBe('*')

    // secret 已配置提示挂在 label-desc（与高级页一致的描述位）
    const desc = rows[1].querySelector('.label-desc')
    expect(desc.textContent).toContain('已配置')
    expect(desc.textContent).toContain('留空保持不变')
    // secret 是密码框
    const secretInput = rows[1].querySelector('.setting-input')
    expect(secretInput.type).toBe('password')

    // 保存按钮走全局 btn-primary（save-bar 容器）
    expect(document.querySelector('.modal-container .save-bar .btn-primary')).toBeTruthy()

    wrapper.unmount()
  })

  it('无配置插件弹窗显示「无可配置项」而非表单', async () => {
    const wrapper = mount(PluginManageView, { attachTo: document.body })
    await flushPromises()
    await wrapper.findAll('button').filter(b => b.text() === '详情')[1].trigger('click')
    await flushPromises()

    expect(document.querySelector('.modal-container .config-none').textContent)
      .toContain('此插件无可配置项')
    expect(document.querySelectorAll('.modal-container .setting-row').length).toBe(0)

    wrapper.unmount()
  })
})
