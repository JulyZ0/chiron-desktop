'use strict'
/**
 * `pickDirectory`（项目绑定根目录）的结果翻译。
 *
 * 为什么不复用 `pickDocumentResult` 的测试：两者语义不同 —— 选的是一次性要读的
 * 文档，还是要长期绑定的项目根目录。合并成一份测试会让「目录选择的路径必须原样
 * 保留」这条约定被文档选择的用例掩盖，而这条约定正是后端 `validateRootPath` 能
 * 正常工作的前提（前端裁剪 `C:\` 会变成 `C:`，后端就不认了）。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')

const { pickDirectoryResult } = require('../shared.cjs')

/** 用平台分隔符拼路径，避免 Windows 上 `path.basename('a/b')` 得到整串。 */
function dir(...parts) {
  return path.join(path.sep, ...parts)
}

test('正常选中一个文件夹：返回 path 与末级目录名', () => {
  const folder = dir('work', 'chiron-desktop')
  const picked = pickDirectoryResult({ canceled: false, filePaths: [folder] })
  assert.deepEqual(picked, { path: folder, name: 'chiron-desktop' })
  // 契约只允许这两个键：多带字段就等于把文件系统信息漏给渲染进程
  assert.deepEqual(Object.keys(picked).sort(), ['name', 'path'])
})

test('用户取消：返回 null（不是错误，调用方按「没选」处理）', () => {
  assert.equal(pickDirectoryResult({ canceled: true, filePaths: [] }), null)
})

test('取消时即使带着路径也返回 null（以 canceled 为准）', () => {
  assert.equal(pickDirectoryResult({ canceled: true, filePaths: [dir('work')] }), null)
})

test('路径原样保留：不做尾部分隔符裁剪，也不展开或规范化', () => {
  const withSlash = dir('work', 'proj') + path.sep
  const picked = pickDirectoryResult({ canceled: false, filePaths: [withSlash] })
  // 关键断言：path 必须逐字符等于对话框给的字符串。
  // 后端 `validateRootPath` 负责规范化；前端先动手会破坏盘符根目录（`C:\` → `C:`）。
  assert.equal(picked.path, withSlash)
})

test('盘符根目录 `C:\\` 不被裁剪成 `C:`', () => {
  const root = `${path.sep === '/' ? '/' : 'C:'}${path.sep}`
  const picked = pickDirectoryResult({ canceled: false, filePaths: [root] })
  assert.equal(picked.path, root)
  // 末级目录名取不到时回退成路径本身，不能是空串 ——
  // 侧边栏标题为空会让项目行看起来像渲染失败
  assert.equal(picked.name, root)
})

test('多选只取第一个：契约返回类型是单个对象', () => {
  const first = dir('work', 'a')
  const picked = pickDirectoryResult({ canceled: false, filePaths: [first, dir('work', 'b')] })
  assert.deepEqual(picked, { path: first, name: 'a' })
})

test('空 filePaths / 缺字段 / 空结果都不伪装成「选中了」', () => {
  assert.equal(pickDirectoryResult({ canceled: false, filePaths: [] }), null)
  assert.equal(pickDirectoryResult({ canceled: false }), null)
  assert.equal(pickDirectoryResult({ canceled: false, filePaths: [''] }), null)
  assert.equal(pickDirectoryResult(null), null)
  assert.equal(pickDirectoryResult(undefined), null)
})
