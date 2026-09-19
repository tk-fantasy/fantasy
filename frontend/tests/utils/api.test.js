/**
 * extractApiError 单元测试。
 *
 * 起因：注册密码不足 8 位时 FastAPI 返回 422，detail 是校验错误对象数组；
 * useAuth 里 `new Error(json.detail)` 把数组强转成 "[object Object]"
 * 直接渲染到了登录页表单上。
 */
import { describe, it, expect } from 'vitest'
import { extractApiError } from '../../src/utils/api'

describe('extractApiError', () => {
  it('422 校验错误数组（密码过短）转为可读中文', () => {
    const json = {
      detail: [{
        type: 'string_too_short',
        loc: ['body', 'password'],
        msg: 'String should have at least 8 characters',
        ctx: { min_length: 8 },
      }],
    }
    expect(extractApiError(json, '注册失败')).toBe('密码长度不足（至少 8 位）')
  })

  it('422 校验错误数组（用户名过长）带上下文长度', () => {
    const json = {
      detail: [{
        type: 'string_too_long',
        loc: ['body', 'username'],
        msg: 'String should have at most 32 characters',
        ctx: { max_length: 32 },
      }],
    }
    expect(extractApiError(json, '注册失败')).toBe('用户名过长（最多 32 位）')
  })

  it('未知字段名时长度提示仍可读（不显示 undefined）', () => {
    const json = {
      detail: [{ type: 'string_too_short', loc: ['body', 'whatever'], msg: 'x', ctx: {} }],
    }
    expect(extractApiError(json, '注册失败')).toMatch(/长度不足/)
    expect(extractApiError(json, '注册失败')).not.toContain('undefined')
  })

  it('缺失必填字段时提示补填', () => {
    const json = {
      detail: [{ type: 'missing', loc: ['body', 'code'], msg: 'Field required' }],
    }
    expect(extractApiError(json, '注册失败')).toBe('请填写邀请码')
  })

  it('detail 为字符串时直接使用', () => {
    expect(extractApiError({ detail: 'Not Found' }, '注册失败')).toBe('Not Found')
  })

  it('AppException 形态（code+message）取 message', () => {
    const json = { code: 'registration_code_invalid', message: '邀请码无效或已被使用', data: null }
    expect(extractApiError(json, '注册失败')).toBe('邀请码无效或已被使用')
  })

  it('未知校验类型回退原始英文 msg', () => {
    const json = { detail: [{ type: 'value_error', loc: ['body', 'x'], msg: 'bad value' }] }
    expect(extractApiError(json, '注册失败')).toBe('bad value')
  })

  it('无法识别的形状回退默认文案', () => {
    expect(extractApiError({}, '注册失败')).toBe('注册失败')
    expect(extractApiError(null, '注册失败')).toBe('注册失败')
    expect(extractApiError(undefined, '登录失败')).toBe('登录失败')
  })
})
