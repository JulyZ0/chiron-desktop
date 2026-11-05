/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * 开发环境登录绕过开关（`1` 开启）。仅 DEV 构建读取，生产构建下
   * `isDevAuthBypassEnabled()` 恒为 false。
   */
  readonly VITE_DEV_AUTH_BYPASS?: string
  /**
   * 绕过模式下用于**真实登录**的开发账号（写进 `.env.local`，该文件不进版本库）。
   * 未配置时绕过只放宽前端守卫，不会尝试登录。
   */
  readonly VITE_DEV_AUTH_EMAIL?: string
  readonly VITE_DEV_AUTH_PASSWORD?: string
}

// 全局性能数据声明 (仅供开发调试)
interface Window {
  __ChironPerf?: {
    mark: (name: string) => void
    measure: (name: string, startMark: string, endMark: string) => number | null
    getReport: () => {
      lcp: number | null
      fid: number | null
      cls: number | null
      fcp: number | null
      ttfb: number | null
      firstRenderMs: number
    }
  }
}

// Navigator.connection 类型声明
interface Navigator {
  connection?: {
    effectiveType: string
    downlink: number
    saveData: boolean
    rtt: number
    type: string
  }
}

// Performance memory 类型声明 (Chrome only)
interface Performance {
  memory?: {
    usedJSHeapSize: number
    totalJSHeapSize: number
    jsHeapSizeLimit: number
  }
}