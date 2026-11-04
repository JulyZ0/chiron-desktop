/**
 * 日期时间格式化（i18n 统一入口）。
 *
 * 为什么集中到这里：项目里原有 20+ 处 `new Date(x).toLocaleString('zh-CN', …)` ——
 * locale 写死在调用点，界面切到 en-US / ar 之后日期仍是中文格式。日期与界面语言不一致
 * 是 i18n 最常见的漏网之鱼（比按钮文案更容易被忽略，因为它在表格/列表里）。
 *
 * dayjs 的 locale 由 i18n 的 setLocale() 同步（见 src/i18n/index.ts），因此本模块
 * **不接收 locale 参数**：格式化结果自动跟随界面语言与书写方向。
 */
import dayjs from 'dayjs'

type DateInput = string | number | Date | null | undefined

function parse(value: DateInput): dayjs.Dayjs | null {
  if (value === null || value === undefined || value === '') return null
  const d = dayjs(value)
  return d.isValid() ? d : null
}

/** 完整日期时间，如 `2026-09-15 10:49`（列表/表格默认用这个） */
export function formatDateTime(value: DateInput, fallback = '-'): string {
  const d = parse(value)
  return d ? d.format('YYYY-MM-DD HH:mm') : fallback
}

/** 仅日期，如 `2026-09-15` */
export function formatDate(value: DateInput, fallback = '-'): string {
  const d = parse(value)
  return d ? d.format('YYYY-MM-DD') : fallback
}

/** 仅时间（24 小时制），如 `10:49` */
export function formatTime(value: DateInput, fallback = '-'): string {
  const d = parse(value)
  return d ? d.format('HH:mm') : fallback
}

/** 紧凑的月日（列表内联显示）：zh-CN `9月15日` / en-US `Sep 15` */
export function formatMonthDay(value: DateInput, fallback = '-'): string {
  const d = parse(value)
  return d ? d.format('MMM D') : fallback
}

/** 相对/绝对混合（列表时间列常用）：今天只显示时间，否则显示月日+时间 */
export function formatSmart(value: DateInput, fallback = '-'): string {
  const d = parse(value)
  if (!d) return fallback
  return d.isSame(dayjs(), 'day') ? d.format('HH:mm') : d.format('MMM D HH:mm')
}
