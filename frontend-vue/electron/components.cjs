/**
 * 本地组件管理（core / pageindex / langextract）。
 *
 * 职责划分：
 *   - **desktop-local 不再是 HTTP 服务**：它只剩数据栈引导器
 *     （`python -m app.launcher`，只起 embedded PostgreSQL 与 Redis）。过去那条
 *     `python -m app.main` 起 FastAPI core 的老路径已随业务层删除，本文件因此不再
 *     拥有「启动本地后端」的职责 —— 本地后端一律由 `local-stack.cjs` 拉起。
 *   - **pageindex / langextract** 的安装、启停全部委托给后端的 `/v1/components*`
 *     端点（实现在 `desktop-local/app/components/`）：本文件不再自己下载、解压或
 *     spawn，只做 HTTP 调用 + 错误翻译 + 状态缓存。
 *   - 目录 `userData/components/<name>` 仍然存在，但**只用于 core 的二进制与回退展示**；
 *     pageindex / langextract 的真实安装目录以 core 的 data-dir 为准。
 *   - core 没在运行时不给“假装已安装”的答案：列表回退到磁盘探测并标 `error`，
 *     install/start/stop 直接抛“本地 core 未运行”，绝不静默成功。
 *   - spawn 一律 `stdio: 'pipe'`：stdout/stderr 只累计字节数；stderr 仅保留末尾
 *     一小段给**报错消息**（脱敏 + 截断），不落盘、不打印整段，避免把本地后端的
 *     请求体/密钥刷进日志。
 *   - 启动参数只走环境变量 + 少量 CLI（见 buildCoreEnv / resolveCoreLaunch）：
 *     `CHIRON_*` 是 contracts.md §3.1 的契约名，`CHIRON_LOCAL_*` 是当前
 *     desktop-local/app/config.py（pydantic-settings 的 env_prefix）真正读的名字，
 *     两套一起下发，见 docs/desktop-local/handshake.md。
 */
const fs = require('node:fs')
const net = require('node:net')
const path = require('node:path')
const crypto = require('node:crypto')
const childProcess = require('node:child_process')
const { spawn } = childProcess
const DEFAULTS = require('./defaults.cjs')
// 探测代码只写一份：首次安装（runtime-install.cjs）与启动前挑选解释器共用
const { probeRuntime } = require('./runtime-probe.cjs')

/** 与 contracts.md 的 ComponentInfo.name 一致，顺序即 UI 展示顺序 */
const COMPONENT_NAMES = DEFAULTS.COMPONENT_NAMES

/** 单一来源：默认值都在 defaults.cjs，这里只做别名，不在本文件写字面量 */
const CORE_HEALTH_TIMEOUT_MS = DEFAULTS.CORE_HEALTH_TIMEOUT_MS
const CORE_READY_TIMEOUT_MS = DEFAULTS.CORE_READY_TIMEOUT_MS
const CORE_HEALTH_INTERVAL_MS = DEFAULTS.CORE_HEALTH_INTERVAL_MS
const CORE_STOP_TIMEOUT_MS = DEFAULTS.CORE_STOP_TIMEOUT_MS
const CORE_KILL_TIMEOUT_MS = DEFAULTS.CORE_KILL_TIMEOUT_MS
/** 报错消息里最多带多少字节 stderr（脱敏后）：够定位 ImportError / 端口占用即可 */
const CORE_STDERR_TAIL_BYTES = DEFAULTS.CORE_STDERR_TAIL_BYTES
/** core 的组件端点（desktop-local/app/api/components.py） */
const CORE_COMPONENTS_PATH = DEFAULTS.CORE_COMPONENTS_PATH
/** 列组件应该很快：卡住就别让 UI 转圈 */
const CORE_LIST_TIMEOUT_MS = DEFAULTS.CORE_LIST_TIMEOUT_MS
/** 安装要复制/解压组件包，给宽一点 */
const CORE_INSTALL_TIMEOUT_MS = DEFAULTS.CORE_INSTALL_TIMEOUT_MS
/** 启动可能要等健康检查（core 侧默认 20s），再留点余量 */
const CORE_ACTION_TIMEOUT_MS = DEFAULTS.CORE_ACTION_TIMEOUT_MS
/** 启动尝试次数（含首次）：端口冲突 / 提前退出 / 就绪超时都会换端口重试 */
const CORE_PORT_ATTEMPTS = DEFAULTS.CORE_PORT_ATTEMPTS

/**
 * core 的启动环境变量（contracts.md §3.1 的**契约名**）。
 * 值只在这里出现一次，其它地方引用这个表，避免字符串散落。
 */
const CORE_ENV = Object.freeze({
  dataDir: 'CHIRON_DATA_DIR',
  port: 'CHIRON_PORT',
  deviceToken: 'CHIRON_DEVICE_TOKEN',
  inferenceProxy: 'CHIRON_INFERENCE_PROXY',
  python: 'CHIRON_PYTHON',
})

/**
 * 契约名 → desktop-local/app/config.py 实际读的名字。
 *
 * config.py 是 pydantic-settings，`env_prefix = "CHIRON_LOCAL_"`，所以真正生效的是
 * `CHIRON_LOCAL_DATA_DIR` / `_PORT` / `_INFERENCE_PROXY`；只写契约名会被静默忽略
 * （表现为 core 用了平台默认数据目录、随机端口，壳层却以为参数已生效）。
 * 两套一起写是当前最松的耦合：等 core 直接支持契约名，把这张表删掉即可。
 * `desktop-local/app/**` 不在本 worktree 的改动范围内，所以桥接放在这一侧。
 */
const CORE_ENV_ALIASES = Object.freeze({
  CHIRON_DATA_DIR: ['CHIRON_LOCAL_DATA_DIR'],
  CHIRON_PORT: ['CHIRON_LOCAL_PORT'],
  CHIRON_INFERENCE_PROXY: ['CHIRON_LOCAL_INFERENCE_PROXY'],
  // auth.py 从 <data-dir>/auth/device-token 读设备 token，暂不读 env；先照样下发，
  // 等 core 支持后无需再改这一侧。
  CHIRON_DEVICE_TOKEN: [],
})

const state = {
  /** 当前 core 进程记录；null 表示本进程没起过或已停 */
  core: null,
  /**
   * pageindex / langextract 的运行状态缓存，内容来自 core 的 `/v1/components`。
   * 真进程由 core 管理，这里只用来在 core 短暂不可达时也能给出最后一次已知状态。
   */
  children: new Map(),
}

let injected = null

function runtime() {
  if (injected) return { spawn, spawnSync: childProcess.spawnSync, ...injected }
  const electron = require('electron')
  return { app: electron.app, spawn, spawnSync: childProcess.spawnSync }
}

/** 仅供 __tests__（纯 Node，无 electron）：注入 app.getPath */
function _setDeps(deps) {
  injected = deps || null
}

function _resetDeps() {
  injected = null
}

/** 仅供 __tests__：清掉进程记录与组件状态缓存（不会去 kill 真进程） */
function _resetState() {
  state.core = null
  state.children.clear()
  // 运行时探测缓存是模块级的：不清掉会让下一条用例看到上一条的解释器结论
  runtimeProbeCache.clear()
}

function userDataDir() {
  // 与 config.cjs 同一优先级：显式覆盖 > Electron 的 userData。
  // 真机 smoke / 多环境隔离都靠它把 config、本地数据、组件目录放进同一个临时根。
  const override = String(process.env.CHIRON_DESKTOP_USER_DATA || '').trim()
  if (override) return override
  const { app } = runtime()
  if (!app || typeof app.getPath !== 'function') {
    throw new Error('components.cjs 需要 Electron 的 app.getPath（纯 Node 环境请注入依赖）')
  }
  return app.getPath('userData')
}

function componentsRoot() {
  return path.join(userDataDir(), 'components')
}

function assertComponentName(name) {
  const target = String(name || '')
  if (!COMPONENT_NAMES.includes(target)) {
    throw new Error(`未知组件：${name}（可选：${COMPONENT_NAMES.join(' / ')}）`)
  }
  return target
}

function componentDir(name) {
  return path.join(componentsRoot(), assertComponentName(name))
}

/** manifest.json 读不出来（缺失 / 坏 JSON / 不是对象）就当未安装 */
async function readManifest(dir) {
  try {
    const raw = await fs.promises.readFile(path.join(dir, 'manifest.json'), 'utf8')
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    return parsed
  } catch {
    return null
  }
}

/** 目录体积；符号链接跳过（防止环），读不到的条目忽略 */
async function dirSize(dir) {
  let entries
  try {
    entries = await fs.promises.readdir(dir, { withFileTypes: true })
  } catch {
    return 0
  }
  let total = 0
  for (const entry of entries) {
    const full = path.join(dir, entry.name)
    try {
      if (entry.isDirectory()) total += await dirSize(full)
      else if (entry.isFile()) total += (await fs.promises.stat(full)).size
    } catch {
      // 权限/竞态：忽略这一项，返回已统计到的字节数
    }
  }
  return total
}

function repoRoot() {
  // frontend-vue/electron/components.cjs → 仓库根
  return path.resolve(__dirname, '..', '..')
}

/** desktop-local 目录：`-m app.launcher` 的 cwd，也是 `app` 包能被导入的前提 */
function coreSourceDir(root = repoRoot()) {
  return path.join(root, 'desktop-local')
}

/**
 * 打包后 desktop-local 的位置：`extraResources` 把它放在 `resources/desktop-local`。
 * `__dirname` 在 app.asar 内，不能再用 `path.resolve(__dirname, '..', '..')`。
 */
function packagedCoreDir() {
  if (!process.resourcesPath) return ''
  const candidate = path.join(process.resourcesPath, 'desktop-local')
  // 判据是**引导器**入口：`app/main.py`（FastAPI core）已随业务层删除，
  // 还拿它当判据会让打包态被整体误判成"开发态"—— 运行时安装与网关链路全部失效。
  return fs.existsSync(path.join(candidate, 'app', 'launcher.py')) ? candidate : ''
}

/**
 * 只读查询：本地运行时现在能不能直接用（**core 与引擎两套环境都算**）。
 *
 * 主进程在建主窗口**之前**用它决定"要不要先弹一个安装进度窗口"——
 * 正常启动（运行时已就绪）就不该闪一个进度窗出来。
 *
 * 为什么必须两套一起判：装了网关之后本地链路是 `引导器 → python-engine → Go 网关`，
 * 引擎起不来整条链就起不来。只看 core 会让进度窗在"core 装好了、引擎从没装过"时
 * 根本不出现，于是安装无处显示、失败也无处显示 —— 正是本轮要消掉的静默降级。
 */
function inspectLocalRuntime() {
  let userData
  try {
    userData = userDataDir()
  } catch {
    // 纯 Node 环境（单测）没有 app.getPath：当作"无从判断"，不要因此挡住启动
    return { ready: false, reason: 'no-userdata', message: '无法确定本地数据目录' }
  }
  const sources = packagedCoreDir()
  if (!sources) {
    // 开发态：运行时由开发者本机负责，界面不需要提示安装
    return { ready: true, reason: 'dev', message: '' }
  }
  const installer = require('./runtime-install.cjs')
  // 两套环境的改绑都得报出去：core 修好之后就往下判引擎，返回值只带**引擎**的 binding，
  // 于是 core 那次自愈在日志里一个字都不留（真机验证时就是这么丢的）。
  const rebinds = []
  const keep = (result) => {
    if (result && result.binding && result.binding.rebound) rebinds.push(result.binding)
    return result
  }
  const core = keep(
    installer.inspectRuntime({
      target: 'core',
      userDataPath: userData,
      sources,
      probe: probeBundledRuntime,
    }),
  )
  // 先报 core：缺 uv / 缺解释器这类问题两套环境是同一个原因，先把这一环修好，下一轮才轮到引擎
  if (!core.ready) return { ...core, rebinds }
  return { ...keep(inspectEngineRuntime(userData)), rebinds }
}

/**
 * python-engine 随包源码目录。**空串 = 开发态**（仓库里那份由开发者本机环境负责）。
 *
 * 判据是 `app/main.py` 存在：光有目录不算，那可能是个空壳。
 */
function packagedEngineDir() {
  if (!process.resourcesPath) return ''
  const candidate = path.join(process.resourcesPath, 'python-engine')
  return fs.existsSync(path.join(candidate, 'app', 'main.py')) ? candidate : ''
}

/** 开发态的引擎目录（仓库根下的 python-engine） */
function devEngineDir() {
  const candidate = path.join(repoRoot(), 'python-engine')
  return fs.existsSync(path.join(candidate, 'app', 'main.py')) ? candidate : ''
}

/**
 * 只读查询：AI 引擎的环境能不能直接用。**开发态恒就绪** ——
 * 那里没有"随包引擎源码"，装环境既无处可装，也不该去动用户自己的解释器。
 */
function inspectEngineRuntime(userDataPath) {
  const sources = packagedEngineDir()
  if (!sources) return { ready: true, reason: 'dev', message: '' }
  let userData = String(userDataPath || '')
  if (!userData) {
    try {
      userData = userDataDir()
    } catch {
      return { ready: false, reason: 'no-userdata', message: '无法确定本地数据目录' }
    }
  }
  const installer = require('./runtime-install.cjs')
  return installer.inspectRuntime({
    target: 'engine',
    userDataPath: userData,
    sources,
    probe: probeBundledRuntime,
  })
}

/**
 * 本地运行时**只有一个来源**：首次启动由随包 CPython 建 venv 并装依赖到
 * `<userData>/local/runtime`。
 *
 * 这里不再查找 `resources/python` 或 `resources/python-runtime` 里的**现成环境**：
 * 随包的是裸解释器，依赖必须按当前 `pyproject.toml` 现装，才不会把构建机状态冻进包里。
 */

/**
 * 首次启动时自建的运行时解释器（`<userData>/local/runtime/venv`）。
 *
 * 候选链里它排在**最前**：新包不再随带任何解释器，这份是按当前 `pyproject.toml`
 * 装出来的、能跟随后续依赖变更，理应优先（见 `runtime-install.cjs`）。保留的
 * `resources/python` 只为兼容旧安装包，排在其后。不存在时返回空串（不猜）。
 */
function installedRuntimePython() {
  let base
  try {
    base = userDataDir()
  } catch {
    // 纯 Node 环境没注入 app.getPath：当作没有自建运行时，交给后面的候选
    return ''
  }
  if (!base) return ''
  const venv = path.join(base, 'local', 'runtime', 'venv')
  const exe =
    process.platform === 'win32'
      ? path.join(venv, 'Scripts', 'python.exe')
      : path.join(venv, 'bin', 'python3')
  return fs.existsSync(exe) ? exe : ''
}

/** core 二进制在 userData 里的固定位置（装好的 core 优先用它） */
function coreBinaryPath() {
  const exeName = process.platform === 'win32' ? 'chiron-local.exe' : 'chiron-local'
  return path.join(componentsRoot(), 'core', exeName)
}

/**
 * 打包态运行时自检：解释器在，且关键依赖能 import。
 *
 * **为什么必须有这一步**：原先找不到随包 Python 时会静默回退到 `'python'`。
 * 用户机上这有三种坏结果，而且**都表现为"所有页面报服务繁忙"**：
 *   1. 根本没有 `python` → spawn ENOENT，报错对用户毫无指导性；
 *   2. 有 Microsoft Store 占位符 → spawn 成功但立刻退出；
 *   3. 有真 python 但没装 fastapi/asyncpg → `ModuleNotFoundError`。
 * 与其让用户猜，不如把"缺什么、怎么修"直接讲清楚。
 *
 * 成功结果按解释器路径缓存（一次启动只探测一次）；**失败不缓存**，
 * 用户重装运行时候重试必须能成功。
 */
const runtimeProbeCache = new Map()

/**
 * 打包态"能不能起 core"的唯一判据：**真实 import 一遍 `app` 包**。
 *
 * 探测代码本身在 `runtime-probe.cjs` —— 首次安装（runtime-install.cjs）验收时用的是
 * 同一份实现。两处各写一份就会出现"装的时候判好了、启动时却 import 失败"的自相矛盾，
 * 而上一轮 `httpx` 事故正是这个形态（自检用手维护清单、清单漏项 ⇒ 自检绿、运行时红）。
 *
 * 这里只加一层**按解释器缓存**：一次启动里同一个解释器不重复 import（约 1s/次）。
 */
function probeBundledRuntime(pythonPath, cwd = '', label = '本地核心') {
  if (!pythonPath) return { ok: false, reason: 'missing', message: '未找到随包 Python 运行时' }
  // 缓存键要带 cwd：同一个解释器在不同工作目录下能 import 到的 `app` 包不一样
  const cacheKey = `${pythonPath}\u0000${cwd}`
  const cached = runtimeProbeCache.get(cacheKey)
  if (cached) return cached

  const probe = runtime().spawnSync
  if (typeof probe !== 'function') {
    // 拿不到 spawnSync 就不要假装通过：让后续真实启动去暴露问题
    return { ok: true, reason: 'unverified', message: '' }
  }

  const result = probeRuntime(probe, pythonPath, cwd)
  if (result.ok) {
    runtimeProbeCache.set(cacheKey, result)
    return result
  }

  return {
    ...result,
    // 文案里带路径：回退链上可能同时有多个候选，只说"随包运行时坏了"会误导
    // 也带目标名：core 与引擎共用同一份探测代码，不说清是哪一个在失败就没法照着修
    message: `解释器 ${pythonPath} 无法导入${label}：${result.message || '未知原因'}`,
  }
}

/** 把运行时问题翻成用户能照做的引导（不说"服务繁忙"这种无从下手的废话） */
/**
 * 失败指引：每条都要能让用户（或开发者）真的做点什么。
 *
 * 措辞对齐 Phase 4 —— 运行时不再随包预装，而是首次启动由 uv 装到 userData 里，
 * 所以"重新安装"不再是万能答案：多数情况重试或换镜像源就能好。
 */
function runtimeGuidance(probe) {
  const lines = [probe.message || '本地运行时不可用']
  if (probe.reason === 'sources-missing') {
    lines.push(
      '安装包内缺少本地核心源码（resources/desktop-local）。这是安装包不完整，请重新安装。',
      '开发者请确认打包前 desktop-local/ 存在且 electron-builder.yml 的 extraResources 覆盖了它。',
    )
  } else if (probe.reason === 'missing' || probe.reason === 'venv-missing') {
    lines.push(
      '本地 Python 环境尚未建好。首次启动会用随包解释器建环境并按依赖清单安装；',
      '若装不起来，多半是取依赖失败 —— 设好 CHIRON_PIP_INDEX_URL 指向可用镜像后重启会重试。',
      '开发者也可以设 CHIRON_PYTHON 指向一个已装好依赖的解释器，跳过自动安装。',
    )
  } else if (probe.reason === 'missing-dependency') {
    lines.push(
      '本地环境里缺少依赖包。重启应用会自动补齐（依赖声明变过时会重建环境）；',
      '网络不佳时可设置 `CHIRON_PIP_INDEX_URL` 指向可用镜像源后再重启。',
    )
  } else {
    lines.push(
      '可以在应用内点"重启本地后端"重试；仍失败请把日志（userData/logs/main.log）一起反馈。',
      '开发者可用 `CHIRON_PYTHON` 指定一个已装好依赖的解释器，跳过自动安装。',
    )
  }
  return lines.join('\n')
}

/**
 * 引导器解释器解析：装好的 venv > `CHIRON_PYTHON` > 开发态源码。
 * 都没有就明确报错，不要 spawn 一个不存在的命令（会抛难懂的 ENOENT）。
 * root 可注入，单测用它模拟"仓库里还没有 desktop-local"。
 *
 * 返回 `{ command, args, kind, cwd }`。**主路径只消费 `command` 与 `cwd`**
 * （main.cjs 的 tryStartGoStack → local-stack.startLocalStack 自己拼 `-m app.launcher`）。
 *
 * 入口一律 `-m app.launcher` + `cwd=desktop-local`，**不能**写成
 * `python desktop-local/app/launcher.py`：launcher 是包内导入（`from app import ...`），
 * 以脚本方式启动时 sys.path[0] 是 `desktop-local/app`，`app` 包不在搜索路径上，
 * 必然 ImportError。`-m` 会把 cwd 放进 sys.path。
 */
function resolveCoreLaunch(root = repoRoot()) {
  const exe = coreBinaryPath()
  if (fs.existsSync(exe)) return { command: exe, args: [], kind: 'binary', cwd: root }

  // 打包态：desktop-local 在 resources 下，Python 运行时建在 userData
  const packaged = packagedCoreDir()
  if (packaged) {
    // 候选链（依次探测，**谁真的能 import 依赖就用谁**）：
    //   1. 自建 venv（userData/local/runtime）—— 唯一正常来源，首次启动用随包解释器装出来
    //   2. CHIRON_PYTHON —— 用户/开发者显式指定的解释器
    //
    // **刻意不再兜底到 PATH 上的 python**：那会让"运行时没装"被一个恰好在 PATH 上、
    // 又碰巧装了依赖的解释器掩盖过去 —— 安装流程因此永不触发，用户看不到
    // 创建环境/安装依赖的进度，换台机器就整体失效。缺失就必须走安装，不许蒙混。
    // 想用自家 Python 请显式设 CHIRON_PYTHON，那样是用户的选择而不是静默回退。
    //
    // 包里那份 `resources/python-runtime` 是**裸解释器**，不是现成环境：
    // 它没有依赖，直接拿它启动必然 ModuleNotFoundError。它的用途只有一个 ——
    // 交给 runtime-install.cjs 建 venv。所以候选链里不出现它。
    const attempted = []
    // 自建 venv 排在最前：它按当前 pyproject.toml 装出来，能跟随后续依赖变更
    const installed = installedRuntimePython()
    if (!installed) {
      // 一个都没有：先记一条，保证最终引导指向"运行时还没装"
      attempted.push({
        python: 'userData/local/runtime/venv',
        probe: {
          ok: false,
          reason: 'venv-missing',
          message: '本地运行时尚未创建（正常情况下首次启动会按需下载组件并安装）',
        },
      })
    }
    for (const candidate of [installed, process.env[CORE_ENV.python]]) {
      const python = String(candidate || '').trim()
      if (!python) continue
      if (attempted.some(item => item.python === python)) continue
      // 传 packaged 作 cwd：探测必须和将来 `-m app.launcher` 在同一目录下做，
      // 否则 `import app` 找的不是随包那份源码，探测等于没做。
      const probe = probeBundledRuntime(python, packaged)
      attempted.push({ python, probe })
      if (!probe.ok) continue
      if (python !== installed) {
        // 用了 CHIRON_PYTHON 要留痕：否则排查时无法解释"为什么没用自建那份"
        process.stderr.write(
          `[chiron] 未用自建运行时，改用 ${python}（已确认能 import 随包 desktop-local）\n`,
        )
      }
      return { command: python, args: ['-m', 'app.launcher'], kind: 'packaged', cwd: packaged }
    }

    const detail = attempted.map(item => `  - ${item.python}：${item.probe.message}`).join('\n')
    // 引导以"随包那份"的问题为准：它才是安装包本该提供的东西
    const guidance = runtimeGuidance((attempted[0] && attempted[0].probe) || {})
    const error = new Error(
      `本地运行时不可用（已尝试 ${attempted.length} 个解释器）：\n${detail}\n\n${guidance}`,
    )
    error.retryable = false
    error.guidance = guidance
    error.runtimeAttempts = attempted
    throw error
  }

  const script = path.join(root, 'desktop-local', 'app', 'launcher.py')
  if (fs.existsSync(script)) {
    return {
      command: process.env[CORE_ENV.python] || 'python',
      args: ['-m', 'app.launcher'],
      kind: 'dev',
      cwd: coreSourceDir(root),
    }
  }
  throw new Error(
    `未找到本地引导器：请安装 ${exe}，或保留 desktop-local/app/launcher.py 以开发态启动`,
  )
}

/**
 * 首次启动自动安装运行时。**只在打包态、且安装包确实带了 uv 时才动手。**
 *
 * 返回 `true` 表示"装完了，可以再试一次 resolveCoreLaunch"；返回 `false` 表示这条
 * 路径不适用（开发态、或安装包缺 uv），调用方应把原始的运行时错误抛出去。
 *
 * 为什么失败时**抛安装错误而不是 return false**：两者对用户是不同的事故。
 * 「没有 uv」= 安装包不完整（重新安装）；「有 uv 但装不上」= 网络/磁盘问题（重试或
 * 换源）。用同一个错误盖住会让人无从下手。
 */
let installInFlight = null

async function installRuntimeIfPossible(root, options = {}) {
  // 开发态（没有 resources/desktop-local）不参与：那由开发者的本机环境负责
  const sources = packagedCoreDir()
  if (!sources) return false
  // uv 与 CPython 都随包：缺哪个都说明安装包没按布局打好，
  // ensureRuntime 会明确指出"重新安装"，这里不做闸门判断。
  const installer = require('./runtime-install.cjs')

  let userData
  try {
    userData = userDataDir()
  } catch {
    return false
  }

  // 并发去重：多次 startCore（切换模式、窗口重建）不该同时装两遍 venv，
  // 两个 uv 同时写同一个目录会互相破坏
  if (installInFlight) {
    await installInFlight
    return true
  }

  installInFlight = installer.ensureRuntime({
    userDataPath: userData,
    sources,
    probe: probeBundledRuntime,
    onStage: typeof options.onRuntimeStage === 'function' ? options.onRuntimeStage : undefined,
    onLine: typeof options.onRuntimeLine === 'function' ? options.onRuntimeLine : undefined,
    force: options.reinstallRuntime === true,
    // 下载入口必须跟着依赖注入走，否则单测会真的联网取 uv（几十秒 + 拉 15MB）
    fetch: runtime().fetch,
  })

  try {
    await installInFlight
  } catch (err) {
    const error = new Error(
      `本地运行时自动安装失败：${err && err.message ? err.message : err}\n\n` +
        '这是首次启动必须完成的一步，没有它本地功能无法使用。\n' +
        '常见原因：\n' +
        '  - 网络不通或过慢：可设 CHIRON_PIP_INDEX_URL 指向可用的镜像源后重启；\n' +
        '  - 磁盘空间不足（运行时约 400MB）：清理后重启应用会自动重试；\n' +
        '  - 安全软件拦截了 uv：请放行后重启。',
    )
    error.retryable = false
    error.installFailed = true
    throw error
  } finally {
    installInFlight = null
  }

  // 装完必须清探测缓存：否则启动前那次"缺依赖"的结论还在缓存里，会立刻再失败一次
  runtimeProbeCache.clear()
  return true
}

/**
 * 拿到 core 的启动信息；**打包态运行时还没装时就地装一次**再拿。
 *
 * 为什么需要这一层：`tryStartGoStack` 原先只看 `resolveCoreLaunch()` 抛不抛，
 * 一抛就返回 null 让老路径接手。全新安装恰好就是"还没装"，于是第一次启动永远
 * 落在 desktop-local core 上 —— 少掉网关那一半业务端点，而且引擎依赖压根没机会装
 * （`prepareEngine` 在那之后）。用户要等到第二次启动才拿到功能完整的界面。
 *
 * 装不了的情况保持原样把错误抛回调用方：开发态（没有随包源码）与安装包残缺
 * 都不该由这里兜着。
 */
async function prepareCoreLaunch(options = {}) {
  const root = options.repoRoot || repoRoot()
  let firstError
  try {
    return resolveCoreLaunch(root)
  } catch (err) {
    firstError = err
  }
  if (!(await installRuntimeIfPossible(root, options))) throw firstError
  return resolveCoreLaunch(root)
}

/**
 * **按需**准备 python-engine 的依赖（"用到才装"）。
 *
 * 为什么不在首启无条件装：引擎的依赖闭包比 core 大一个数量级（pydantic-ai /
 * anthropic / openai / pymupdf / grpcio…，实测几百 MB），而它只在**本地模式**用得上。
 * 云端模式的用户为它付一次下载和几百 MB 磁盘，换来的东西一行代码都不会跑。
 * 所以触发点是"本地模式真要起 Go 栈了"，即 `tryStartGoStack` 起引擎之前。
 *
 * 与 `installRuntimeIfPossible` 同一套契约：
 *   - 已经就绪时一条命令都不跑（正常开机走这条）；
 *   - 失败**抛错**并带可照做的指引 —— "安装包残缺"与"装不上"是两种事故；
 *   - 并发去重，避免两个 uv 同时写同一个目录互相破坏；
 *   - 装完清探测缓存，否则"缺依赖"那次结论还在缓存里，会立刻再失败一次。
 */
let engineInstallInFlight = null

async function ensureEngineRuntime(options = {}) {
  const sources = packagedEngineDir()
  if (!sources) return { ok: true, reason: 'dev', venvPython: '', installed: false }

  let userData = String(options.userDataPath || '')
  if (!userData) {
    try {
      userData = userDataDir()
    } catch {
      // 没有 userData 就没有能放 venv 的地方：这不是"可以跳过"的情况，
      // 因为下一步 resolveEngineLaunch 一定会拿不到解释器。
      throw new Error('无法确定本地数据目录，python-engine 的依赖无处可装')
    }
  }

  const installer = require('./runtime-install.cjs')
  const inspect = () =>
    installer.inspectRuntime({
      target: 'engine',
      userDataPath: userData,
      sources,
      probe: probeBundledRuntime,
    })

  const current = inspect()
  if (current.ready) return { ok: true, reason: 'ok', venvPython: current.venvPython, installed: false }

  if (engineInstallInFlight) {
    await engineInstallInFlight
    const after = inspect()
    return {
      ok: after.ready,
      reason: after.ready ? 'reused-install' : 'reused-install-failed',
      venvPython: after.venvPython,
      installed: true,
      message: after.message,
    }
  }

  engineInstallInFlight = installer.ensureRuntime({
    target: 'engine',
    userDataPath: userData,
    sources,
    probe: probeBundledRuntime,
    onStage: typeof options.onRuntimeStage === 'function' ? options.onRuntimeStage : undefined,
    onLine: typeof options.onRuntimeLine === 'function' ? options.onRuntimeLine : undefined,
    force: options.reinstallEngine === true,
  })

  try {
    const result = await engineInstallInFlight
    runtimeProbeCache.clear()
    return { ok: true, reason: 'installed', venvPython: result.venvPython, installed: true }
  } catch (err) {
    const message = err && err.message ? err.message : String(err)
    const error = new Error(
      `AI 引擎的依赖没能装好：${message}\n\n` +
        '本地模式的聊天、知识库、记忆与图谱都由 python-engine 实现，没有它这些功能都不可用。\n' +
        '常见原因：\n' +
        '  - 网络不通或过慢：可设 CHIRON_PIP_INDEX_URL 指向可用的镜像源后重启；\n' +
        '  - 磁盘空间不足（引擎环境约需 1GB）：清理后重启应用会自动重试；\n' +
        '  - 安全软件拦截了 uv：请放行后重启。',
    )
    error.reason = 'engine-deps-install-failed'
    throw error
  } finally {
    engineInstallInFlight = null
  }
}

/**
 * 起 python-engine 该用哪个解释器 —— 以及它到底能不能起。
 *
 * **打包态只认引擎自己那份 venv**（外加显式的 `CHIRON_PYTHON`）。这里刻意不留回退链：
 *   - 回退到引导器 venv：那份按 `desktop-local/pyproject.toml` 装，从来没有引擎依赖，
 *     选中它的唯一结果是 `ModuleNotFoundError`；
 *   - 回退到 PATH 上的 `python`：用户机上可能是 Store 占位符，也可能什么都没装。
 * 这两条回退过去都在，而它们的失败最终被 `tryStartGoStack` 吞掉、静默回落到
 * desktop-local core —— 用户拿到的是"只有部分页面能用"的残缺界面，且零提示。
 * 现在失败带着原因往上抛，留着回退只会把那条原因丢掉。
 *
 * 开发态照旧用本机环境（`CHIRON_PYTHON` 优先，否则 `python`）：那里不该由壳去装环境。
 */
function resolveEngineLaunch(options = {}) {
  const explicit = String(process.env.CHIRON_PYTHON || '').trim()

  const packaged = packagedEngineDir()
  if (packaged) {
    let userData = String(options.userDataPath || '')
    if (!userData) {
      try {
        userData = userDataDir()
      } catch {
        return null
      }
    }
    const installer = require('./runtime-install.cjs')
    const venv = installer.venvPython(userData, 'engine')
    const command = explicit || (fs.existsSync(venv) ? venv : '')
    // 没有可用解释器时返回 null（不猜）：调用方要么先跑 ensureEngineRuntime，
    // 要么把"引擎未就位"如实报出去 —— 绝不能拿一个注定 import 失败的命令去 spawn。
    if (!command) return null
    return { command, args: ['-m', 'app.main'], cwd: packaged, kind: 'packaged' }
  }

  const dev = devEngineDir()
  if (!dev) return null
  return { command: explicit || 'python', args: ['-m', 'app.main'], cwd: dev, kind: 'dev' }
}

/**
 * 组装 core 的环境变量：进程环境 → 调用方覆盖 → 契约名 + config.py 真名。
 * 空值不下发（pydantic 会把 `""` 当成显式值，反而盖掉合理默认）。
 */
function buildCoreEnv(options = {}) {
  const env = { ...process.env, ...(options.env || {}) }
  const set = (name, value) => {
    if (value === undefined || value === null || value === '') return
    env[name] = String(value)
    for (const alias of CORE_ENV_ALIASES[name] || []) env[alias] = String(value)
  }
  set(CORE_ENV.dataDir, options.dataDir)
  set(CORE_ENV.port, options.port)
  set(CORE_ENV.deviceToken, options.token)
  set(CORE_ENV.inferenceProxy, options.inferenceProxyUrl)
  // 桌面端默认开启设备 token 鉴权：前端本地模式会带 Authorization，SSE 走 ?token=。
  // 显式传 authEnabled:false 可关闭（测试/调试）。
  if (!env.CHIRON_LOCAL_AUTH_ENABLED) {
    env.CHIRON_LOCAL_AUTH_ENABLED = options.authEnabled === false ? '0' : '1'
  }
  return env
}

/** dev 态额外下发的 CLI 参数：与 env 同义，但 CLI 优先级更高（launcher.py 的 argparse 覆盖） */
function coreCliArgs(options = {}) {
  const args = []
  if (options.dataDir) args.push('--data-dir', String(options.dataDir))
  if (options.port !== undefined && options.port !== null) args.push('--port', String(options.port))
  if (options.inferenceProxyUrl) args.push('--inference-proxy', String(options.inferenceProxyUrl))
  return args
}

/** 按行切分流式输出：JSON 播报可能被 chunk 截断，必须自己攒行 */
function createLineReader(onLine) {
  let buffer = ''
  return chunk => {
    buffer += chunk
    let index = buffer.indexOf('\n')
    while (index >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, '')
      buffer = buffer.slice(index + 1)
      if (line.trim()) onLine(line)
      index = buffer.indexOf('\n')
    }
    // 单行异常长（比如 core 打了巨大 traceback）就别无限攒内存
    if (buffer.length > 64 * 1024) buffer = buffer.slice(-4096)
  }
}

function tailOf(text, max) {
  const value = String(text || '')
  return value.length <= max ? value : value.slice(-max)
}

/**
 * 报错消息会进 main.log 与用户眼前的对话框，所以 DSN 里的口令、token 之类的
 * `key=value` 必须先抹掉：stderr 里的 Python traceback 经常带完整连接串。
 */
function redactSecrets(text) {
  return String(text || '')
    .replace(/(\b[a-z][a-z0-9+.-]*:\/\/[^/\s:@]+):([^/\s@]+)@/gi, '$1:***@')
    .replace(/(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*["']?[^\s"',;]+/gi, '$1=***')
}


/** 让内核自己开 127.0.0.1:0 拿端口，再立刻关掉——只借端口号，不占坑 */
function pickFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function killProcessTree(child, pid) {
  const injectedKill = injected && typeof injected.killTree === 'function' ? injected.killTree : null
  if (injectedKill) return injectedKill(child, pid) !== false
  if (process.platform !== 'win32') return false
  if (!child || typeof child.spawnfile !== 'string' || !child.spawnfile) return false
  const target = Number.isInteger(pid) && pid > 0 ? pid : child.pid
  if (!Number.isInteger(target) || target <= 0) return false
  try {
    const result = childProcess.spawnSync('taskkill', ['/PID', String(target), '/T', '/F'], {
      windowsHide: true,
      timeout: CORE_KILL_TIMEOUT_MS,
    })
    // 0 = 全部终止；128 = 进程已不存在；其它退出码不算成功（下面还会 SIGTERM/SIGKILL）
    return Boolean(result) && result.status === 0
  } catch {
    return false
  }
}

/** 统一的 fetch 入口：单测通过 `_setDeps({fetch})` 注入，运行期用全局 fetch */
function httpFetch() {
  const injected = runtime()
  return injected.fetch || fetch
}

module.exports = {
  COMPONENT_NAMES,
  CORE_HEALTH_TIMEOUT_MS,
  CORE_ENV,
  CORE_ENV_ALIASES,
  /** 默认值来源（端口/host/重试次数/超时/组件名） */
  DEFAULTS,
  CORE_PORT_ATTEMPTS,
  isPortConflict: DEFAULTS.isPortConflict,
  killProcessTree,
  pickFreePort,
  componentsRoot,
  componentDir,
  coreBinaryPath,
  coreSourceDir,
  resolveCoreLaunch,
  /** 打包态"先按需装好运行时、再给启动信息"：主进程起 Go 栈前用它 */
  prepareCoreLaunch,
  buildCoreEnv,
  coreCliArgs,
  redactSecrets,
  /** 打包态运行时探测：需要真实跑解释器，导出供端到端验证（单测里用假 spawnSync 替身） */
  probeBundledRuntime,
  /** 只读查询运行时是否就绪：主进程据此决定要不要先弹安装进度窗 */
  inspectLocalRuntime,
  /** AI 引擎环境：只读查询 / 按需安装 / 解释器解析 */
  inspectEngineRuntime,
  ensureEngineRuntime,
  resolveEngineLaunch,
  packagedEngineDir,
  _setDeps,
  _resetDeps,
  _resetState,
}
