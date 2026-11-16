import { describe, expect, it } from 'vitest'
import { isAbsolutePath, pathKey, projectRelative } from '../paths'

describe('workbench/paths（项目路径归一化）', () => {
  it('把根内的绝对路径映射成相对路径', () => {
    expect(projectRelative('D:\\proj\\src\\a.ts', 'D:\\proj').rel).toBe('src/a.ts')
    expect(projectRelative('/home/u/proj/src/a.ts', '/home/u/proj').rel).toBe('src/a.ts')
  })

  it('根末尾的斜杠不影响匹配', () => {
    expect(projectRelative('/home/u/proj/src/a.ts', '/home/u/proj/')).toEqual({
      rel: 'src/a.ts',
      inside: true,
    })
  })

  it('Windows 上大小写不敏感', () => {
    // 盘符与目录名都可能是不同大小写，判成"项目外"会让点开文件时凭空报错
    expect(projectRelative('d:/PROJ/src/a.ts', 'D:/proj').rel).toBe('src/a.ts')
  })

  it('项目外的绝对路径标记为 outside', () => {
    expect(projectRelative('/other/place/a.ts', '/home/u/proj')).toEqual({ rel: '', inside: false })
  })

  it('前缀相同但不同目录不算项目内', () => {
    // /home/u/proj-extra 不是 /home/u/proj 的子目录 —— 只比字符串前缀会误判
    expect(projectRelative('/home/u/proj-extra/a.ts', '/home/u/proj').inside).toBe(false)
  })

  it('相对路径一律视为项目内并去掉 ./', () => {
    expect(projectRelative('./src/a.ts', '')).toEqual({ rel: 'src/a.ts', inside: true })
    expect(projectRelative('src/a.ts', '')).toEqual({ rel: 'src/a.ts', inside: true })
  })

  it('根未知时，绝对路径按项目外处理', () => {
    // 树还没加载过就没有根可以比。宁可暂时不标，也不能把别人的文件算进来
    expect(projectRelative('D:/proj/src/a.ts', '').inside).toBe(false)
  })

  it('空输入不是路径', () => {
    expect(projectRelative('', '/home/u/proj')).toEqual({ rel: '', inside: true })
  })

  it('识别绝对路径的各种写法', () => {
    expect(isAbsolutePath('D:/a')).toBe(true)
    expect(isAbsolutePath('C:\\a')).toBe(true)
    expect(isAbsolutePath('/a')).toBe(true)
    expect(isAbsolutePath('\\\\server\\share')).toBe(true)
    expect(isAbsolutePath('src/a.ts')).toBe(false)
  })

  it('比较键抹平大小写与分隔符', () => {
    expect(pathKey('D:\\Proj\\Src\\A.ts')).toBe('d:/proj/src/a.ts')
    expect(pathKey('./src/a.ts')).toBe('src/a.ts')
  })
})
