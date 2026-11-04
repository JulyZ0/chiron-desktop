/// <reference types="vite/client" />

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