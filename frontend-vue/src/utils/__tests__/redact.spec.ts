/**
 * H62：前端出口脱敏（PRD 9.9）。
 *
 * 缺陷形态：`console.*` 与错误提示直接打出服务端/Provider 原文，以及**整个 Axios
 * 错误对象** —— 后者包含 `config.headers.Authorization`。用户只要打开一次 DevTools、
 * 或把报错截图贴进工单，Bearer token 就跟着出去；崩溃报告/前端日志上报同理。
 *
 * 与服务端 `app/subagent/redact.py` 同口径（形态规则一致、标记一致），另外多一条
 * "结构化对象里剔除 Authorization/Cookie 头"，因为前端拿到的是对象而不是文本。
 */
import { describe, it, expect } from 'vitest'
import { REDACTED, redactText, redactUnknown, summarizeApiError } from '../redact'

describe('redactText (H62)', () => {
  it.each([
    ['sk-abcdefghijklmnopqrstuvwxyz012345', 'openai_key'],
    ['sk-ant-abcdefghijklmnopqrstuvwxyz01', 'anthropic_key'],
    ['ghp_0123456789abcdefghijklmnopqrstuvwxyz', 'github_token'],
    ['AKIAIOSFODNN7EXAMPLE', 'aws_access_key'],
    ['Bearer abcdefghijklmnopqrstuvwxyz0123', 'bearer_token'],
  ])('密钥形态被抹掉：%s', (secret, kind) => {
    const out = redactText(`error: ${secret}`)
    expect(out).not.toContain(secret)
    expect(out).toContain(`[REDACTED:${kind}]`)
  })

  it('JSON 密钥字段被抹掉（大小写不敏感）', () => {
    const out = redactText('{"password": "hunter2secret", "API_KEY": "abcdefgh12345678"}')
    expect(out).not.toContain('hunter2secret')
    expect(out).not.toContain('abcdefgh12345678')
    expect(out).toContain('[REDACTED:json_secret_field]')
  })

  it('URL 里的凭据被抹掉', () => {
    const out = redactText('GET https://alice:s3cr3tpass@api.example.com/v1/x failed')
    expect(out).not.toContain('s3cr3tpass')
    expect(out).toContain('[REDACTED:url_credentials]')
  })

  it('私钥块整体抹掉', () => {
    const pem = '-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----'
    const out = redactText(`config: ${pem}`)
    expect(out).not.toContain('MIIE')
  })

  it('幂等：已经脱敏的文本再脱一次不变', () => {
    const once = redactText('key=sk-abcdefghijklmnopqrstuvwxyz012345')
    expect(redactText(once)).toBe(once)
  })

  it('干净文本原样返回；空前缀不炸', () => {
    expect(redactText('compiled 3 files')).toBe('compiled 3 files')
    expect(redactText('')).toBe('')
    expect(redactText(null)).toBe('')
  })
})

describe('redactUnknown (H62：Axios 错误对象)', () => {
  it('Authentication 头整体替换，不保留任何片段', () => {
    const err = {
      message: 'Request failed with status code 401',
      config: { headers: { Authorization: 'Bearer sk-abcdefghijklmnopqrstuvwxyz012345', Accept: 'json' } },
    }
    const out = redactUnknown(err) as { config: { headers: Record<string, string> } }
    expect(out.config.headers.Authorization).toBe(REDACTED)
    expect(out.config.headers.Accept).toBe('json')
    // 原对象不被修改（脱敏不该有副作用）
    expect(err.config.headers.Authorization).toContain('Bearer')
  })

  it('Cookie / x-api-key 同样抹掉', () => {
    const out = redactUnknown({
      headers: { Cookie: 'session=abc', 'X-Api-Key': 'secret-key-value', 'User-Agent': 'x' },
    }) as { headers: Record<string, string> }
    expect(out.headers.Cookie).toBe(REDACTED)
    expect(out.headers['X-Api-Key']).toBe(REDACTED)
    expect(out.headers['User-Agent']).toBe('x')
  })

  it('嵌套响应体里的密钥字段也被脱敏', () => {
    const out = redactUnknown({ response: { data: { detail: 'bad key sk-abcdefghijklmnopqrstuvwxyz012345' } } })
    expect(JSON.stringify(out)).not.toContain('sk-abcdefghijklmnopqrstuvwxyz012345')
  })

  it('深度过大时折叠成标记（避免循环引用把控制台打爆）', () => {
    const deep: Record<string, unknown> = {}
    let cursor = deep
    for (let i = 0; i < 20; i += 1) {
      const next: Record<string, unknown> = {}
      cursor.next = next
      cursor = next
    }
    expect(JSON.stringify(redactUnknown(deep))).toContain(REDACTED)
  })

  it('基本类型与 null 原样返回', () => {
    expect(redactUnknown(1)).toBe(1)
    expect(redactUnknown(true)).toBe(true)
    expect(redactUnknown(null)).toBe(null)
  })
})

describe('summarizeApiError (H62：上报只带可诊断信息)', () => {
  it('抽出 status / code / request-id，正文脱敏', () => {
    const summary = summarizeApiError({
      code: 'ERR_BAD_RESPONSE',
      message: 'failed with Bearer sk-abcdefghijklmnopqrstuvwxyz012345',
      response: { status: 401, headers: { 'x-request-id': 'req-123' } },
    })
    expect(summary.status).toBe(401)
    expect(summary.code).toBe('ERR_BAD_RESPONSE')
    expect(summary.requestId).toBe('req-123')
    expect(summary.message).not.toContain('sk-abcdefghijklmnopqrstuvwxyz012345')
  })

  it('没有响应时给出 0 而不是 undefined', () => {
    const summary = summarizeApiError(new Error('network down'))
    expect(summary.status).toBe(0)
    expect(summary.requestId).toBe('')
  })

  it('空值不炸', () => {
    expect(() => summarizeApiError(undefined)).not.toThrow()
  })
})
