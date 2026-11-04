import { beforeEach, describe, expect, it } from 'vitest'
import { clearFolds, readFolds, replaceFolds, writeFold } from '../transcriptFolds'

const SESSION = 's1'

beforeEach(() => {
  localStorage.clear()
})

describe('transcriptFolds（折叠状态持久化）', () => {
  it('写入后按会话读回', () => {
    writeFold(SESSION, 't:turn-1', false)
    writeFold(SESSION, 't:turn-1:g0', true)
    const folds = readFolds(SESSION)
    expect(folds.get('t:turn-1')).toBe(false)
    expect(folds.get('t:turn-1:g0')).toBe(true)
  })

  it('会话之间互不影响', () => {
    writeFold('s1', 't:a', false)
    writeFold('s2', 't:a', true)
    expect(readFolds('s1').get('t:a')).toBe(false)
    expect(readFolds('s2').get('t:a')).toBe(true)
  })

  it('未记住的会话返回空表', () => {
    expect(readFolds('never-written').size).toBe(0)
  })

  it('同一会话超出上限时淘汰最早写入的条目', () => {
    for (let i = 0; i < 105; i++) writeFold(SESSION, `t:turn-${i}`, true)
    const folds = readFolds(SESSION)
    expect(folds.size).toBe(100)
    expect(folds.has('t:turn-0')).toBe(false)
    expect(folds.has('t:turn-104')).toBe(true)
  })

  it('重复写入同一折叠状态会把它移到最近（LRU），不会挤掉别人', () => {
    for (let i = 0; i < 100; i++) writeFold(SESSION, `t:turn-${i}`, true)
    writeFold(SESSION, 't:turn-0', false)     // 重新激活第 0 条
    writeFold(SESSION, 't:turn-100', true)    // 触发一次淘汰
    const folds = readFolds(SESSION)
    expect(folds.get('t:turn-0')).toBe(false)
    expect(folds.has('t:turn-1')).toBe(false) // 被淘汰的是最早未更新的那条
  })

  it('存储内容损坏时降级为空表而不是抛异常', () => {
    localStorage.setItem('chiron:transcript-folds:v1', '{ not json')
    expect(readFolds(SESSION).size).toBe(0)
  })

  it('replaceFolds 覆盖该会话，clearFolds 清空该会话', () => {
    writeFold(SESSION, 't:old', true)
    replaceFolds(SESSION, new Map([['t:new', false]]))
    expect(readFolds(SESSION).has('t:old')).toBe(false)
    expect(readFolds(SESSION).get('t:new')).toBe(false)
    clearFolds(SESSION)
    expect(readFolds(SESSION).size).toBe(0)
  })
})
