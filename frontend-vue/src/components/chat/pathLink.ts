/**
 * 正文里的「这看起来像个项目文件路径」判定。
 *
 * 背景：助手回答里经常写着 `src/api/index.ts`、`internal/api/project_files.go` 这样的
 * 路径。用户看到后的第一个动作是"打开看看"，而当前只能自己去面板里一层层点。
 *
 * ## 为什么只在**行内代码**上做，而不是扫正文全文
 *
 * 扫全文会把普通英文里的 `README.md`、`a.b` 之类也变成链接，正文立刻变成一片可点
 * 区域 —— 点错一次就打开个工作台，比不能点更烦。Markdown 作者用反引号包住路径，
 * 本身就是在说"这是一段字面量"，这是唯一可靠的信号。
 *
 * ## 为什么这里不校验"文件是否真的存在"
 *
 * 判定必须是**纯函数**：点击处理在事件回调里同步跑，发请求再判断会让点击延迟几百
 * 毫秒，用户会以为没点上。存在性由工作台判断 —— 它拿到项目根，找不到时给明确提示。
 */

/** 结尾一段必须带扩展名：`src/a.ts` 算，`src/a` / `npm test` / `foo` 不算 */
const HAS_EXT = /(^|[\\/])[^\\/\s]+\.[A-Za-z0-9]{1,8}$/

/** 正文里出现这些内容的行内代码是语法/命令/URL，不是文件 */
const NOT_A_PATH = /^(https?:|mailto:|data:|git@|\/\/)/i

export function isLikelyProjectPath(text: string): boolean {
  const value = String(text || '').trim()
  if (!value || value.length > 180) return false
  // 含空白的一律不认：正文里的行内代码常是命令（`npm run build`）或表达式，
  // 而真实路径带空格时也无从判断边界 —— 猜错不如不猜。
  if (/\s/.test(value)) return false
  if (NOT_A_PATH.test(value)) return false
  return HAS_EXT.test(value)
}
