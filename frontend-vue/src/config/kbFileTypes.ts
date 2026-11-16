/**
 * 知识库原文类型白名单（前端副本）。
 *
 * 真正决定"能不能入库"的是网关：`internal/api/kb_file_types.go`。
 * 这里只负责 UX —— 让用户在选文件的那一刻就知道哪些不会被接受，
 * 而不是传完 64MB 才收到 400。
 *
 * 三份清单必须一致：本文件、Go 侧 kb_file_types.go、Python 侧
 * `app/knowledge/remote.py` 的 `_LOCAL_SUFFIXES` / `_CLOUD_SUFFIXES`。
 * `internal/api/kb_file_types_test.go` 会同时读前后端两处源码做比对，
 * 任何一侧单独改动都会让测试变红。
 */
export const KB_SELF_HOSTED_SUFFIXES = ['.pdf'] as const
export const KB_CLOUD_SUFFIXES = ['.doc', '.docx', '.pdf', '.ppt', '.pptx'] as const

/** 该 provider 接受的后缀。未知 provider 按并集放行，与网关同口径。 */
export function kbSupportedSuffixes(provider?: string | null): readonly string[] {
  if (provider === 'selfhosted') return KB_SELF_HOSTED_SUFFIXES
  if (provider === 'cloud') return KB_CLOUD_SUFFIXES
  return Array.from(
    new Set<string>([...KB_SELF_HOSTED_SUFFIXES, ...KB_CLOUD_SUFFIXES]),
  ).sort()
}

/**
 * 按文件名判断是否受支持。
 *
 * 无扩展名一律拒绝：网关侧已不再用 "ext 为空就当成 txt" 兜底（那等于给任意
 * 文件开后门），前端判定必须同口径，否则会出现"选文件时放行、上传后被拒"。
 */
export function isSupportedKbFile(
  provider: string | null | undefined,
  name: string,
): boolean {
  const dot = name.lastIndexOf('.')
  if (dot < 0) return false
  const ext = name.slice(dot).toLowerCase()
  return kbSupportedSuffixes(provider).includes(ext)
}

/** 给 `<input type="file" accept>` 用的逗号串。 */
export function kbAcceptAttr(provider?: string | null): string {
  return kbSupportedSuffixes(provider).join(',')
}
