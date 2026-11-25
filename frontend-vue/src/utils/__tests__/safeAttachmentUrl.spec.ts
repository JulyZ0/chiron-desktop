/**
 * H64：附件 URL 的来源与协议约束。
 *
 * 缺陷形态：`file_url` / `url` 由服务端返回，`MessageItem` 之前直接塞进 `:src`/`:href`。
 * 协作/公开分享里的内容于是可以让**浏览者**去执行或请求任意地址：
 *   - `javascript:`（href 点一下就执行）；
 *   - `file:///C:/...`（Electron 下读本机文件）；
 *   - `http://10.0.0.5/admin`（内网探活 / CSRF）；
 *   - `//evil.com/x.png`（协议相对，看着像站内路径）。
 *
 * 策略是**默认拒绝**：只放行站内 `/media/`、`/v1/` 相对路径、同源/显式受信任 origin 的
 * `https:`、回环 `http:`（桌面本地栈）、`blob:` 与图片类 `data:`。
 */
import { describe, it, expect } from 'vitest'
import { sanitizeAttachmentUrl, normalizeAttachmentOrigin, isLoopbackHost } from '../safeAttachmentUrl'

const policy = {
  selfOrigin: 'https://app.example.com',
  trustedOrigins: ['https://cdn.example.com', 'http://127.0.0.1:8080'],
}

describe('sanitizeAttachmentUrl (H64)', () => {
  it('放行站内相对路径', () => {
    expect(sanitizeAttachmentUrl('/media/abc/download', policy)).toBe('/media/abc/download')
    expect(sanitizeAttachmentUrl('/v1/media/abc', policy)).toBe('/v1/media/abc')
  })

  it('拒绝不在白名单里的站内路径（避免把任意同源接口当附件渲染）', () => {
    expect(sanitizeAttachmentUrl('/admin/users', policy)).toBe('')
    expect(sanitizeAttachmentUrl('/api/private', policy)).toBe('')
  })

  it('拒绝协议相对地址（//evil.com 看似站内，实则跨站）', () => {
    expect(sanitizeAttachmentUrl('//evil.example/x.png', policy)).toBe('')
  })

  it('放行同源与受信任 origin 的 https', () => {
    expect(sanitizeAttachmentUrl('https://app.example.com/media/a.png', policy)).toBe(
      'https://app.example.com/media/a.png',
    )
    expect(sanitizeAttachmentUrl('https://cdn.example.com/a.png', policy)).toBe('https://cdn.example.com/a.png')
  })

  it('拒绝未受信任的 https（任意第三方域）', () => {
    expect(sanitizeAttachmentUrl('https://evil.example/a.png', policy)).toBe('')
    expect(sanitizeAttachmentUrl('https://cdn.example.com.evil.io/a.png', policy)).toBe('')
  })

  it('拒绝危险 scheme', () => {
    for (const raw of [
      'javascript:alert(1)',
      'JavaScript:alert(1)',
      'file:///C:/Windows/win.ini',
      'vbscript:msgbox(1)',
      'about:blank',
      'mailto:a@b.c',
    ]) {
      expect(sanitizeAttachmentUrl(raw, policy)).toBe('')
    }
  })

  it('明文 http 只允许回环，且需在受信任集合里', () => {
    expect(sanitizeAttachmentUrl('http://127.0.0.1:8080/media/a.png', policy)).toBe(
      'http://127.0.0.1:8080/media/a.png',
    )
    // 内网地址：即便写了 http 也不放行
    expect(sanitizeAttachmentUrl('http://10.0.0.5/admin', policy)).toBe('')
    // 明文公网域：不放行
    expect(sanitizeAttachmentUrl('http://cdn.example.com/a.png', policy)).toBe('')
  })

  it('allowLoopbackHttp=false 时连回环明文也拒绝', () => {
    expect(
      sanitizeAttachmentUrl('http://127.0.0.1:8080/media/a.png', { ...policy, allowLoopbackHttp: false }),
    ).toBe('')
  })

  it('data: 只允许图片类型', () => {
    expect(sanitizeAttachmentUrl('data:image/png;base64,AAA', policy)).toBe('data:image/png;base64,AAA')
    expect(sanitizeAttachmentUrl('data:text/html,<script>alert(1)</script>', policy)).toBe('')
    expect(sanitizeAttachmentUrl('data:image/svg+xml,<svg onload=alert(1)>', policy)).toBe('')
  })

  it('blob: 放行（应用自己的对象 URL）', () => {
    expect(sanitizeAttachmentUrl('blob:https://app.example.com/1234', policy)).toBe(
      'blob:https://app.example.com/1234',
    )
  })

  it('空值 / 非字符串一律返回空串', () => {
    for (const raw of ['', '   ', null, undefined, 0, {}]) {
      expect(sanitizeAttachmentUrl(raw, policy)).toBe('')
    }
  })

  it('没有配置受信任 origin 时，跨站 https 依然被拒', () => {
    expect(sanitizeAttachmentUrl('https://cdn.example.com/a.png', { selfOrigin: 'https://app.example.com' })).toBe('')
  })
})

describe('normalizeAttachmentOrigin / isLoopbackHost', () => {
  it('规范化到 origin', () => {
    expect(normalizeAttachmentOrigin('https://App.Example.com:443/api?x=1')).toBe('https://app.example.com')
    expect(normalizeAttachmentOrigin('ftp://app.example.com')).toBe('')
    expect(normalizeAttachmentOrigin('garbage')).toBe('')
  })

  it('识别回环', () => {
    expect(isLoopbackHost('127.0.0.1')).toBe(true)
    expect(isLoopbackHost('localhost')).toBe(true)
    expect(isLoopbackHost('10.0.0.5')).toBe(false)
  })
})
