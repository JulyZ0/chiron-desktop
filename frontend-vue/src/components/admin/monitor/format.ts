/**
 * 「运行时监控」的统一格式化口径。
 *
 * 一条硬规矩：**未知就显示「—」，不显示 0**。后端把采不到的指标返回 null，
 * 这里所有函数都把 null / undefined / NaN 收敛成 UNKNOWN —— 否则模板里一个
 * `?? 0` 就会把"Redis 挂了"显示成"命中率 0%"。
 */

/** 未知值的统一占位。 */
export const UNKNOWN = '—'

function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

export function fmtNum(v: number | null | undefined, digits = 0): string {
  if (!isNum(v)) return UNKNOWN
  return v.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function fmtPct(v: number | null | undefined, digits = 1): string {
  return isNum(v) ? `${fmtNum(v, digits)}%` : UNKNOWN
}

export function fmtMs(v: number | null | undefined): string {
  return isNum(v) ? `${fmtNum(v, v >= 100 ? 0 : 1)} ms` : UNKNOWN
}

export function fmtMB(v: number | null | undefined): string {
  if (!isNum(v)) return UNKNOWN
  return v >= 1024 ? `${fmtNum(v / 1024, 2)} GB` : `${fmtNum(v, 1)} MB`
}

/** 秒数转人话：1d 2h 3m / 45s。0 是真实的 0（不是未知），照实显示。 */
export function fmtDuration(seconds: number | null | undefined): string {
  if (!isNum(seconds)) return UNKNOWN
  const s = Math.max(0, Math.round(seconds))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s % 60}s`
  return `${s}s`
}

/** 毫秒级 TTL 转人话（Redis avg_ttl 是毫秒）。 */
export function fmtTTL(ms: number | null | undefined): string {
  if (!isNum(ms)) return UNKNOWN
  return fmtDuration(ms / 1000)
}

/** RFC3339 → 本地时间；解析失败回退原串（不吞掉信息）。 */
export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return UNKNOWN
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString()
}

/** 把任意数值序列清洗成画线可用的数组（丢弃 null/NaN）。 */
export function seriesOf(values: Array<number | null | undefined>): number[] {
  return values.filter(isNum)
}
