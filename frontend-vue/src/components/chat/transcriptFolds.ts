/**
 * 折叠状态持久化（按会话分键）。
 *
 * 为什么按会话分键：折叠是「我在这个会话里读到哪儿」的阅读状态；跨会话沿用会让新会话
 * 继承上一会话的展开态，看起来像随机展开。读写失败（隐私模式、配额满）静默降级为内存态，
 * 折叠态本来就不是必须持久化的数据，不值得为它打断渲染。
 */

import type { FoldKey } from './transcriptProjection'

const STORAGE_KEY = 'chiron:transcript-folds:v1'
/** 单会话最多记住多少条折叠状态，防止长会话把 localStorage 撑满 */
const MAX_FOLDS_PER_SESSION = 100

type SessionFolds = Record<string, boolean>
type FoldStore = Record<string, SessionFolds>

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null   // Safari 隐私模式下访问 localStorage 本身会抛异常
  }
}

function readStore(): FoldStore {
  const target = storage()
  if (!target) return {}
  try {
    const raw = target.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? (parsed as FoldStore) : {}
  } catch {
    return {}
  }
}

function writeStore(store: FoldStore): void {
  const target = storage()
  if (!target) return
  try {
    target.setItem(STORAGE_KEY, JSON.stringify(store))
  } catch {
    // 配额满 / 被禁用：放弃持久化，本次会话内的折叠仍然生效（组件持有内存态）
  }
}

export function readFolds(sessionKey: string): Map<FoldKey, boolean> {
  const folds = new Map<FoldKey, boolean>()
  const per = readStore()[sessionKey]
  if (!per) return folds
  for (const [key, open] of Object.entries(per)) folds.set(key, open === true)
  return folds
}

/** 写入单个折叠状态。同一会话超出上限时淘汰最早写入的条目：
 * 重复写入同一 key 先删后插，使它移到最后，于是插入顺序即 LRU。 */
export function writeFold(sessionKey: string, fold: FoldKey, open: boolean): void {
  const store = readStore()
  const per: SessionFolds = { ...(store[sessionKey] ?? {}) }
  delete per[fold]
  per[fold] = open
  const keys = Object.keys(per)
  if (keys.length > MAX_FOLDS_PER_SESSION) {
    for (const stale of keys.slice(0, keys.length - MAX_FOLDS_PER_SESSION)) delete per[stale]
  }
  store[sessionKey] = per
  writeStore(store)
}

export function replaceFolds(sessionKey: string, folds: ReadonlyMap<FoldKey, boolean>): void {
  const per: SessionFolds = {}
  for (const [key, open] of folds) per[key] = open
  const store = readStore()
  store[sessionKey] = per
  writeStore(store)
}

export function clearFolds(sessionKey: string): void {
  const store = readStore()
  delete store[sessionKey]
  writeStore(store)
}
