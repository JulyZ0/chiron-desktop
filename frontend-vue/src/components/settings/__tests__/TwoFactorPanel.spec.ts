import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 两步验证面板：钉住三条**安全相关**的界面契约。
 *
 * 1. **恢复码必须停下来让用户抄写** —— 验证器丢失时它是唯一出路，用一个 toast
 *    打发等于把用户锁在门外；
 * 2. **停用 / 重新生成必须走验密码那一步** —— 否则旁人拿到未锁的窗口就能把第二
 *    因素关掉，这道防线等于不存在；
 * 3. **shell 不支持时如实说"不支持"**，不能显示成"未启用"（那会诱导用户以为
 *    自己关着它，而实际是根本没有）。
 *
 * 选择器用 `data-test` 钩子：这里断言的是行为开关，不是文案排版。
 */
type FakeStore = Record<string, unknown>

const deviceStoreMock = vi.hoisted(() => ({
  isDesktop: true,
  getTwoFactorStatus: vi.fn(async (): Promise<Record<string, unknown> | null> => ({
    enabled: false,
    enabledAt: '',
    recoveryCodesLeft: 0,
    unreadable: false,
  })),
  beginTwoFactor: vi.fn(async () => ({ ok: true, secret: 'JBSWY3DPEHPK3PXP', otpauth: 'otpauth://totp/x' })),
  confirmTwoFactor: vi.fn(async (): Promise<Record<string, unknown>> => ({
    ok: true,
    recoveryCodes: ['AAAA-BBBB', 'CCCC-DDDD'],
  })),
  // 返回类型显式放宽：成功与失败分支形状不同（失败只有 ok/reason），
  // 让 TS 从首个返回值推断会把它锁成窄类型，后续用例再赋值就报错。
  disableTwoFactor: vi.fn(async (): Promise<Record<string, unknown>> => ({ ok: true })),
  regenerateRecoveryCodes: vi.fn(async (): Promise<Record<string, unknown>> => ({
    ok: true,
    recoveryCodes: ['EEEE-FFFF'],
  })),
}))

const messageMock = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}))

vi.mock('../../../stores/device', () => ({
  useDeviceStore: () => deviceStoreMock as unknown as FakeStore,
}))

vi.mock('ant-design-vue', async importOriginal => {
  const actual = await importOriginal<typeof import('ant-design-vue')>()
  return { ...actual, message: messageMock }
})

// qrcode 在 jsdom 里能跑，但用例不关心图形本身 —— stub 掉避免拖慢
vi.mock('qrcode', () => ({
  default: { toString: async () => '<svg data-test="qr"></svg>' },
}))

import TwoFactorPanel from '../TwoFactorPanel.vue'

function mountPanel() {
  return mount(TwoFactorPanel, {
    global: {
      stubs: {
        'a-alert': { name: 'AAlert', props: ['message'], template: '<div class="alert">{{ message }}</div>' },
        'a-tag': { name: 'ATag', template: '<span><slot /></span>' },
        'a-spin': { name: 'ASpin', template: '<div><slot /></div>' },
        'a-button': {
          name: 'AButton',
          props: ['disabled'],
          emits: ['click'],
          template: '<button type="button" :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
        },
        'a-input': {
          name: 'AInput',
          props: ['value'],
          template: '<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />',
        },
        'a-input-password': {
          name: 'AInputPassword',
          props: ['value'],
          template: '<input type="password" :value="value" @input="$emit(\'update:value\', $event.target.value)" />',
        },
        'a-modal': {
          name: 'AModal',
          props: ['open', 'title', 'closable', 'keyboard'],
          template: '<div v-if="open" class="modal"><slot /></div>',
        },
      },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  deviceStoreMock.isDesktop = true
  deviceStoreMock.getTwoFactorStatus.mockResolvedValue({
    enabled: false,
    enabledAt: '',
    recoveryCodesLeft: 0,
    unreadable: false,
  })
  deviceStoreMock.beginTwoFactor.mockResolvedValue({ ok: true, secret: 'JBSWY3DPEHPK3PXP', otpauth: 'otpauth://totp/x' })
  deviceStoreMock.confirmTwoFactor.mockResolvedValue({ ok: true, recoveryCodes: ['AAAA-BBBB', 'CCCC-DDDD'] })
  deviceStoreMock.disableTwoFactor.mockResolvedValue({ ok: true })
  deviceStoreMock.regenerateRecoveryCodes.mockResolvedValue({ ok: true, recoveryCodes: ['EEEE-FFFF'] })
})

describe('TwoFactorPanel', () => {
  it('未启用时只给「启用」入口，不给停用/重新生成', async () => {
    const w = mountPanel()
    await flushPromises()

    expect(w.find('[data-test="tfa-enable"]').exists()).toBe(true)
    expect(w.find('[data-test="tfa-disable"]').exists()).toBe(false)
    expect(w.find('[data-test="tfa-regenerate"]').exists()).toBe(false)
  })

  it('★★ 启用成功后弹出恢复码（只显示这一次），不是 toast 打发', async () => {
    const w = mountPanel()
    await flushPromises()

    await w.find('[data-test="tfa-enable"]').trigger('click')
    await flushPromises()
    // 候选密钥已经拿到，弹窗里能看到它（手动输入路径）
    expect(w.find('[data-test="tfa-secret"]').text()).toContain('JBSWY3DPEHPK3PXP')

    const input = w.find('[data-test="tfa-confirm-input"]')
    await input.setValue('123456')
    await w.find('[data-test="tfa-confirm"]').trigger('click')
    await flushPromises()

    const codes = w.find('[data-test="tfa-recovery-codes"]')
    expect(codes.exists(), '恢复码必须摆出来让用户抄 —— 丢了验证器时它是唯一出路').toBe(true)
    expect(codes.text()).toContain('AAAA-BBBB')
    expect(codes.text()).toContain('CCCC-DDDD')
  })

  it('★★ 确认期间禁止关闭，迟到成功的一次性恢复码不会丢', async () => {
    let resolveConfirm: ((value: Record<string, unknown>) => void) | undefined
    deviceStoreMock.confirmTwoFactor.mockImplementationOnce(() => new Promise(resolve => {
      resolveConfirm = resolve
    }))
    const w = mountPanel()
    await flushPromises()
    await w.find('[data-test="tfa-enable"]').trigger('click')
    await flushPromises()
    await w.find('[data-test="tfa-confirm-input"]').setValue('123456')
    await w.find('[data-test="tfa-confirm"]').trigger('click')
    await flushPromises()

    const enrollModal = w.findAllComponents({ name: 'AModal' })
      .find(modal => modal.attributes('data-test') === 'tfa-enroll-modal')
    expect(enrollModal).toBeDefined()
    expect(enrollModal!.props('closable')).toBe(false)
    expect(enrollModal!.props('keyboard')).toBe(false)
    enrollModal!.vm.$emit('update:open', false)
    await flushPromises()

    expect(w.find('[data-test="tfa-enroll-modal"]').exists()).toBe(true)
    resolveConfirm?.({ ok: true, recoveryCodes: ['ONLY-CODE'] })
    await flushPromises()
    expect(w.find('[data-test="tfa-recovery-codes"]').text()).toContain('ONLY-CODE')
  })

  it('取消启用会清掉候选密钥、二维码和验证码', async () => {
    const w = mountPanel()
    await flushPromises()
    await w.find('[data-test="tfa-enable"]').trigger('click')
    await flushPromises()
    await w.find('[data-test="tfa-confirm-input"]').setValue('123456')

    await w.find('[data-test="tfa-enroll-cancel"]').trigger('click')
    await flushPromises()
    deviceStoreMock.beginTwoFactor.mockResolvedValueOnce({
      ok: true, secret: 'NEW-ENROLLMENT-SECRET', otpauth: 'otpauth://totp/new',
    })
    await w.find('[data-test="tfa-enable"]').trigger('click')
    await flushPromises()
    expect(deviceStoreMock.beginTwoFactor).toHaveBeenCalledTimes(2)

    expect(w.find('[data-test="tfa-secret"]').text()).toContain('NEW-ENROLLMENT-SECRET')
    expect(w.find('[data-test="tfa-secret"]').text()).not.toContain('JBSWY3DPEHPK3PXP')
    expect(w.find('[data-test="tfa-confirm-input"]').element).toHaveProperty('value', '')
  })

  it('恢复码弹窗通过 X/Escape 关闭时也会清掉恢复码', async () => {
    const w = mountPanel()
    await flushPromises()
    await w.find('[data-test="tfa-enable"]').trigger('click')
    await flushPromises()
    await w.find('[data-test="tfa-confirm-input"]').setValue('123456')
    await w.find('[data-test="tfa-confirm"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-test="tfa-recovery-codes"]').exists()).toBe(true)

    const recoveryModal = w.findAllComponents({ name: 'AModal' })
      .find(modal => modal.attributes('data-test') === 'tfa-recovery-modal')
    expect(recoveryModal).toBeDefined()
    recoveryModal!.vm.$emit('update:open', false)
    await flushPromises()
    deviceStoreMock.confirmTwoFactor.mockResolvedValueOnce({ ok: true, recoveryCodes: ['NEW-CODE'] })
    await w.find('[data-test="tfa-enable"]').trigger('click')
    await flushPromises()
    await w.find('[data-test="tfa-confirm-input"]').setValue('123456')
    await w.find('[data-test="tfa-confirm"]').trigger('click')
    await flushPromises()

    expect(w.find('[data-test="tfa-recovery-codes"]').text()).toContain('NEW-CODE')
    expect(w.find('[data-test="tfa-recovery-codes"]').text()).not.toContain('AAAA-BBBB')
  })

  it('★★ 停用必须先验密码（旁人拿到未锁窗口也关不掉第二因素）', async () => {
    deviceStoreMock.getTwoFactorStatus.mockResolvedValue({
      enabled: true,
      enabledAt: new Date().toISOString(),
      recoveryCodesLeft: 5,
      unreadable: false,
    })
    const w = mountPanel()
    await flushPromises()

    await w.find('[data-test="tfa-disable"]').trigger('click')
    await flushPromises()

    // 直接停用是不能发生的：必须先经过密码弹窗
    expect(deviceStoreMock.disableTwoFactor).not.toHaveBeenCalled()
    const pwd = w.find('[data-test="tfa-password-input"]')
    expect(pwd.exists(), '停用必须有密码输入框').toBe(true)

    await pwd.setValue('correct-password')
    await w.find('[data-test="tfa-password-submit"]').trigger('click')
    await flushPromises()

    expect(deviceStoreMock.disableTwoFactor).toHaveBeenCalledWith('correct-password')
  })

  it('密码错误时不静默失败：就地报错且不关弹窗', async () => {
    deviceStoreMock.getTwoFactorStatus.mockResolvedValue({
      enabled: true,
      enabledAt: '',
      recoveryCodesLeft: 1,
      unreadable: false,
    })
    deviceStoreMock.disableTwoFactor.mockResolvedValue({ ok: false, reason: 'bad-credential' })
    const w = mountPanel()
    await flushPromises()

    await w.find('[data-test="tfa-disable"]').trigger('click')
    await flushPromises()
    await w.find('[data-test="tfa-password-input"]').setValue('wrong')
    await w.find('[data-test="tfa-password-submit"]').trigger('click')
    await flushPromises()

    // 弹窗仍在（还能再试），且给出了原因
    expect(w.find('[data-test="tfa-password-input"]').exists()).toBe(true)
    expect(w.find('[data-test="tfa-password-input"]').element).toHaveProperty('value', '')
    expect(w.text()).toContain('密码不正确')
  })

  it('★ 恢复码用完时给出必须处理的警告（否则验证器一丢就进不来）', async () => {
    deviceStoreMock.getTwoFactorStatus.mockResolvedValue({
      enabled: true,
      enabledAt: '',
      recoveryCodesLeft: 0,
      unreadable: false,
    })
    const w = mountPanel()
    await flushPromises()

    expect(w.text()).toContain('恢复码已用完')
  })

  it('★ 配置读不出来时如实提示，而不是显示成「未启用」', async () => {
    deviceStoreMock.getTwoFactorStatus.mockResolvedValue({
      enabled: false,
      enabledAt: '',
      recoveryCodesLeft: 0,
      unreadable: true,
    })
    const w = mountPanel()
    await flushPromises()

    expect(w.text()).toContain('读不出来')
  })

  it('★ 外壳不支持时明确说"不支持"，不伪装成未启用', async () => {
    deviceStoreMock.getTwoFactorStatus.mockResolvedValue(null)
    const w = mountPanel()
    await flushPromises()

    expect(w.text()).toContain('不支持两步验证')
    expect(w.find('[data-test="tfa-enable"]').exists()).toBe(false)
  })

  it('重新生成恢复码同样要验密码', async () => {
    deviceStoreMock.getTwoFactorStatus.mockResolvedValue({
      enabled: true,
      enabledAt: '',
      recoveryCodesLeft: 3,
      unreadable: false,
    })
    const w = mountPanel()
    await flushPromises()

    await w.find('[data-test="tfa-regenerate"]').trigger('click')
    await flushPromises()
    expect(deviceStoreMock.regenerateRecoveryCodes).not.toHaveBeenCalled()

    await w.find('[data-test="tfa-password-input"]').setValue('pw')
    await w.find('[data-test="tfa-password-submit"]').trigger('click')
    await flushPromises()

    expect(deviceStoreMock.regenerateRecoveryCodes).toHaveBeenCalledWith('pw')
  })
})
