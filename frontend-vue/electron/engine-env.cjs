'use strict'
/**
 * 桌面端可配置的**引擎环境变量**（白名单）。
 *
 * ## 为什么需要它
 *
 * 引擎有一批开关只认环境变量（`python-engine/app/config.py` 的 pydantic-settings，
 * `env_prefix=""`、`case_sensitive=False`，所以字段 `superagent_takeover_enabled`
 * 对应的就是 `SUPERAGENT_TAKEOVER_ENABLED`）。桌面端把引擎当子进程拉起，env 来自
 * `local-stack.cjs::buildEngineEnv` 的 `{...process.env}` —— 用户从开始菜单启动时
 * 根本没有 shell 可以设变量，于是这些开关**在安装包里恒为默认值**。
 *
 * 最直接的后果：`superagent_takeover_enabled` 默认 `false`（引擎不巡检），用户在
 * 界面上配了定时接管也不会被执行，只能"改服务端环境变量再重启"—— 而打包态既没有
 * 服务端也没有 shell。这就是"superagent 永远不可用"的真正原因。
 *
 * ## 为什么是白名单，而不是"用户填 KEY=VALUE"
 *
 * 让用户写任意 env 等于把整个进程的安全边界交出去：
 *   - `APP_SECRET` 是本地模式派生 JWT / INTERNAL_TOKEN 的种子，改掉它就等于换掉
 *     整套鉴权；
 *   - `SANDBOX_BACKEND` / `SANDBOX_ISOLATION_*` 决定执行隔离是否成立，写错一个
 *     字母就是"静默关掉隔离"；
 *   - `PATH` / `PYTHONPATH` 会让引擎加载到别的代码。
 *
 * 所以这里只列**功能开关**（布尔），每个都在引擎侧有默认值、且不影响安全边界。
 * 新增一项的门槛是：它必须是"用户要不要这个能力"，而不是"这台机器怎么运行"。
 *
 * ## 优先级
 *
 * `process.env`（开发者 shell）< 桌面端配置 < `options.env`（单测/ops 注入）。
 * 桌面端配置压在 `process.env` 之上，是因为"界面上明确点过"比"继承来的变量"更能
 * 代表用户意图；而单测需要把值钉死，所以 `options.env` 仍然最高。
 */

const fs = require('node:fs')
const path = require('node:path')

/** 落盘文件名（放在 userData 目录，与 config.json 平级但**不复用**它） */
const ENGINE_ENV_FILE = 'engine-env.json'

/**
 * 白名单。`env` 必须是引擎侧 `Settings` 里真实存在的字段的大写形式 ——
 * 拼错不会报错，只会静默无效（pydantic 的 `extra="ignore"` 让它一路走到运行期）。
 */
const ENGINE_TOGGLES = Object.freeze([
  Object.freeze({
    env: 'SUPERAGENT_TAKEOVER_ENABLED',
    type: 'boolean',
    /** 引擎侧默认值：写在这里是为了让 UI 能显示"默认关"，也便于单测对照 */
    default: false,
  }),
  Object.freeze({
    env: 'MEMORY_DISTILL_ENABLED',
    type: 'boolean',
    default: false,
  }),
])

/** 按 env 名索引，便于校验时 O(1) 查表 */
const BY_ENV = Object.freeze(
  ENGINE_TOGGLES.reduce((acc, item) => {
    acc[item.env] = item
    return acc
  }, {}),
)

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

/**
 * 把用户给的值归一化成该开关的类型；无法识别返回 `undefined`（= 该项不写入）。
 *
 * 布尔刻意接受字符串与数字：配置文件是用户可手改的，`"true"` / `1` 都会出现。
 * 但**不接受** `"false"` 之外的任意字符串 —— 把 `"yes"` 猜成 true 会让一个
 * 明确写错的配置变成"看起来生效了"。
 */
function normalizeValue(type, value) {
  if (type === 'boolean') {
    if (typeof value === 'boolean') return value
    if (value === 1 || value === '1') return true
    if (value === 0 || value === '0') return false
    if (typeof value === 'string') {
      const text = value.trim().toLowerCase()
      if (text === 'true' || text === 'on' || text === 'yes') return true
      if (text === 'false' || text === 'off' || text === 'no') return false
    }
    return undefined
  }
  return undefined
}

/**
 * 过滤并归一化一份开关集合：**白名单之外的键一律丢弃**。
 *
 * 丢弃而不是报错：配置文件可能被更高版本写过（降级安装），也可能是用户手改出错。
 * 让整个设置因为一个未知键而失效，比忽略它更糟。
 */
function normalizeToggles(raw) {
  const src = isPlainObject(raw) ? raw : {}
  const out = {}
  for (const [env, value] of Object.entries(src)) {
    const spec = BY_ENV[env]
    if (!spec) continue
    const normalized = normalizeValue(spec.type, value)
    if (normalized === undefined) continue
    out[env] = normalized
  }
  return out
}

/**
 * 转成 `spawn` 能用的 env 片段。
 *
 * 只输出**用户显式设置过**的键 —— 没设过的留给引擎自己的默认值。显式设成 `false`
 * 也会输出 `'false'`：那表示"我明确关掉"，与"我从没管过"在排查时不是一回事。
 */
function togglesToEnv(toggles) {
  const normalized = normalizeToggles(toggles)
  const env = {}
  for (const [name, value] of Object.entries(normalized)) {
    env[name] = value ? 'true' : 'false'
  }
  return env
}

/**
 * 把开关合进一份已存在的 env（`buildEngineEnv` 的落点）。
 *
 * 不就地改入参：`buildEngineEnv` 的调用方可能复用同一个对象（换端口重试）。
 */
function mergeIntoEnv(baseEnv, toggles) {
  return { ...(isPlainObject(baseEnv) ? baseEnv : {}), ...togglesToEnv(toggles) }
}

function engineEnvPath(dataDir) {
  return path.join(String(dataDir || ''), ENGINE_ENV_FILE)
}

/**
 * 读开关配置。文件不存在 / 损坏 / 字段非法一律当"没配过"，不抛。
 *
 * 与 `config.cjs` 同样的取舍：设置读不出来时应用必须还能启动 —— 引擎用默认值
 * 跑起来，总好过整个应用打不开。
 */
function readEngineEnvSettings(dataDir) {
  const file = engineEnvPath(dataDir)
  let data
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return {}
  }
  return normalizeToggles(isPlainObject(data) ? data.toggles : undefined)
}

/**
 * 写入开关配置（**整体替换**那部分，不合并旧值）。
 *
 * 不做 merge：用户关掉一个开关就是关掉，如果这里合并旧值，界面上取消勾选将永远
 * 撤销不掉。调用方要"只改一项"时，先读出来再整体写回。
 */
function writeEngineEnvSettings(dataDir, toggles) {
  const file = engineEnvPath(dataDir)
  const normalized = normalizeToggles(toggles)
  fs.mkdirSync(String(dataDir || ''), { recursive: true })
  fs.writeFileSync(file, `${JSON.stringify({ toggles: normalized }, null, 2)}\n`, 'utf8')
  return normalized
}

module.exports = {
  ENGINE_ENV_FILE,
  ENGINE_TOGGLES,
  BY_ENV,
  engineEnvPath,
  normalizeToggles,
  togglesToEnv,
  mergeIntoEnv,
  readEngineEnvSettings,
  writeEngineEnvSettings,
}
