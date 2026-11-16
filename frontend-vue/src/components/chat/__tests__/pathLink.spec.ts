import { describe, expect, it } from 'vitest'
import { isLikelyProjectPath } from '../pathLink'

describe('isLikelyProjectPath', () => {
  it('认出带目录的相对路径', () => {
    expect(isLikelyProjectPath('src/api/index.ts')).toBe(true)
    expect(isLikelyProjectPath('internal/api/project_files.go')).toBe(true)
    expect(isLikelyProjectPath('./src/App.vue')).toBe(true)
  })

  it('认出 Windows 绝对路径', () => {
    expect(isLikelyProjectPath('D:\\proj\\src\\a.ts')).toBe(true)
    expect(isLikelyProjectPath('/home/me/proj/src/a.ts')).toBe(true)
  })

  it('认出裸文件名（正文里常直接写 README.md）', () => {
    expect(isLikelyProjectPath('README.md')).toBe(true)
    expect(isLikelyProjectPath('package.json')).toBe(true)
  })

  it('拒绝命令与表达式 —— 行内代码大多是这些', () => {
    expect(isLikelyProjectPath('npm run build')).toBe(false)
    expect(isLikelyProjectPath('const a = 1')).toBe(false)
    expect(isLikelyProjectPath('useWorkbench()')).toBe(false)
  })

  it('拒绝 URL：它们该走链接而不是工作台', () => {
    expect(isLikelyProjectPath('https://example.com/a.ts')).toBe(false)
    expect(isLikelyProjectPath('mailto:a@b.com')).toBe(false)
  })

  it('裸文件名与裸域名都按路径处理 —— 误判代价不对称', () => {
    // example.com 命中"末段带扩展名"的规则。这里**故意**接受它：
    // 误判成路径只会让用户看到一次"项目里找不到"，而拒绝裸文件名
    // （README.md / package.json 在正文里极常见）等于这个功能大面积失效。
    expect(isLikelyProjectPath('example.com')).toBe(true)
    expect(isLikelyProjectPath('docs/example.com')).toBe(true)
  })

  it('拒绝无扩展名的标识符与目录', () => {
    expect(isLikelyProjectPath('node_modules')).toBe(false)
    expect(isLikelyProjectPath('src/api')).toBe(false)
    expect(isLikelyProjectPath('foo')).toBe(false)
  })

  it('拒绝空串与超长文本', () => {
    expect(isLikelyProjectPath('')).toBe(false)
    expect(isLikelyProjectPath('   ')).toBe(false)
    expect(isLikelyProjectPath(`src/${'a'.repeat(200)}.ts`)).toBe(false)
  })

  it('扩展名过长不算路径（避免把 file.name.with.many.dots 当文件）', () => {
    expect(isLikelyProjectPath('a.toolongextension')).toBe(false)
  })
})
