'use strict'
/**
 * H86：受管 CPython **必须固定到补丁版本**，且构建期与运行期必须是同一个版本。
 *
 * 为什么单独立一条用例（PRD 9.10 / H86）：
 *
 * 版本此前只写两位（`3.14`）。构建脚本把两位交给 uv，由 uv 选"当前最新稳定补丁"；
 * 运行时同样只比两位。于是：
 *   - 不同日期打的包，里面装的解释器补丁版本可能不同（干净构建不可复现）；
 *   - 用户机上那份运行时究竟是 3.14.1 还是 3.14.4，制品本身看不出来；
 *   - 供应链审计（"这个包用的到底是哪个解释器"）无从落地。
 *
 * 这里钉住两件事：
 *   1. 两处常量**逐字相等**（构建期与运行期不漂移）；
 *   2. 都是**三段式**补丁版本（不允许再退回两位）。
 *
 * 全程不联网、不跑 uv：只读两个源文件 + 内置常量。
 */
const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const installer = require('../runtime-install.cjs')

/** 从构建脚本里抠出 `RUNTIME_PYTHON_VERSION = "x.y.z"`。 */
function pythonScriptVersion() {
  const file = path.join(__dirname, '..', '..', '..', 'desktop-local', 'scripts', 'prepare_python.py')
  const source = fs.readFileSync(file, 'utf8')
  const m = source.match(/^RUNTIME_PYTHON_VERSION\s*=\s*"([^"]+)"/m)
  assert.ok(m, 'desktop-local/scripts/prepare_python.py 里找不到 RUNTIME_PYTHON_VERSION')
  return m[1]
}

test('运行时版本是补丁级（三段式）', () => {
  const version = installer.RUNTIME_PYTHON_VERSION
  assert.match(
    version,
    /^\d+\.\d+\.\d+$/,
    `必须固定到补丁版本（例如 3.14.4），当前是 ${version} —— 两位版本会让 uv 自行挑补丁，制品因此不可复现`,
  )
})

test('构建期与运行期版本逐字一致', () => {
  assert.equal(
    installer.RUNTIME_PYTHON_VERSION,
    pythonScriptVersion(),
    'electron/runtime-install.cjs 与 desktop-local/scripts/prepare_python.py 的版本必须同时改',
  )
})

test('版本不携带预发布/构建元数据', () => {
  const version = installer.RUNTIME_PYTHON_VERSION
  assert.ok(!/[a-zA-Z+-]/.test(version), `受管解释器只接受正式补丁版本，当前是 ${version}`)
})
