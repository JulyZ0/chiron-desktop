/**
 * 前端出口脱敏（PRD 9.9 / H62）。
 *
 * 缺陷形态：`console.*` 与界面上的错误提示直接打出服务端/Provider 的原文与
 * **整个 Axios 错误对象**。后者包含 `config.headers.Authorization` —— 只要用户
 * 打开一次 DevTools 或把报错截图发出来，Bearer token 就跟着泄漏；浏览器
 * 崩溃报告、前端日志上报同理。
 *
 * 与后端 `app/subagent/redact.py` 同口径（形态规则一致、标记一致），只是这里
 * 多了一条"结构化对象里剔除 Authorization/Cookie 头"，因为前端拿到的是对象而
 * 不是文本。
 */

/** 与服务端一致的脱敏标记。 */
export const REDACTED = '[REDACTED]'

/** 密钥形态（顺序即匹配顺序；与服务端 PATTERNS 对齐）。 */
const PATTERNS: Array<{ kind: string; re: RegExp }> = [
  { kind: 'anthropic_key', re: /sk-ant-[A-Za-z0-9_-]{16,}/g },
  { kind: 'openai_key', re: /\bsk-[A-Za-z0-9_-]{16,}/g },
  { kind: 'github_token', re: /\bgh[pousr]_[A-Za-z0-9]{20,}/g },
  { kind: 'aws_access_key', re: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g },
  { kind: 'google_api_key', re: /\bAIza[0-9A-Za-z_-]{30,}/g },
  { kind: 'slack_token', re: /\bxox[abprs]-[A-Za-z0-9-]{10,}/g },
  { kind: 'bearer_token', re: /\bBearer\s+[A-Za-z0-9._-]{20,}/g },
  { kind: 'jwt', re: /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g },
  { kind: 'private_key_block', re: /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g },
  {
    kind: 'json_secret_field',
    // JS 没有 `(?i)` 内联标志（那是 PCRE/Go 的写法）：大小写不敏感只能靠 `i` 标志。
    re: /"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|client[_-]?secret|secret|token|password|passwd|pwd)"\s*:\s*"[^"]{8,}"/gi,
  },
  {
    kind: 'password_assignment',
    re: /\b(?:password|passwd|pwd)['"]?\s*[:=]\s*["']?[^\s"',]{8,}/gi,
  },
  // URL 里的凭据：`https://user:pass@host`。
  { kind: 'url_credentials', re: /\/\/[^/@\s]+:[^/@\s]+@/g },
]

/** 文本脱敏（幂等：已脱敏的标记不会再被匹配）。 */
export function redactText(input: unknown): string {
  const text = typeof input === 'string' ? input : input == null ? '' : String(input)
  if (!text) return ''
  let out = text
  for (const { kind, re } of PATTERNS) {
    out = out.replace(re, `[REDACTED:${kind}]`)
  }
  return out
}

/** 需要整体抹掉的敏感头（值不进入日志）。 */
const SENSITIVE_HEADERS = new Set(['authorization', 'cookie', 'set-cookie', 'x-api-key', 'proxy-authorization'])

function redactHeaders(headers: unknown): unknown {
  if (!headers || typeof headers !== 'object') return headers
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(headers as Record<string, unknown>)) {
    out[key] = SENSITIVE_HEADERS.has(key.toLowerCase()) ? REDACTED : redactUnknown(value)
  }
  return out
}

/**
 * 结构化脱敏：深拷贝一份可安全打印/上报的对象。
 *
 * 重点处理 Axios 错误：`config.headers.Authorization`、`request.responseURL`
 * （可能带签名 query）、`response.data` 里的密钥字段。
 */
export function redactUnknown(value: unknown, depth = 0): unknown {
  if (depth > 6) return REDACTED
  if (value == null) return value
  if (typeof value === 'string') return redactText(value)
  if (typeof value === 'number' || typeof value === 'boolean') return value
  if (Array.isArray(value)) return value.map(item => redactUnknown(item, depth + 1))
  if (typeof value !== 'object') return String(value)

  const src = value as Record<string, unknown>
  const out: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(src)) {
    const lower = key.toLowerCase()
    if (SENSITIVE_HEADERS.has(lower)) {
      out[key] = REDACTED
      continue
    }
    if (lower === 'headers' || lower === 'config') {
      out[key] = redactHeaders(item)
      continue
    }
    out[key] = redactUnknown(item, depth + 1)
  }
  return out
}

/** 从 Axios 错误里抽出可安全上报的摘要（status / code / request-id / 脱敏正文）。 */
export function summarizeApiError(error: unknown): {
  status: number
  code: string
  requestId: string
  message: string
} {
  const err = (error ?? {}) as {
    response?: { status?: number; data?: unknown; headers?: Record<string, unknown> }
    code?: string
    message?: string
    config?: { headers?: Record<string, unknown> }
  }
  const headers = err.response?.headers ?? {}
  const requestId = String(
    headers['x-request-id'] ?? headers['x-request-id'.toUpperCase()] ?? '',
  )
  return {
    status: Number(err.response?.status) || 0,
    code: String(err.code || ''),
    requestId,
    message: redactText(err.message || ''),
  }
}
