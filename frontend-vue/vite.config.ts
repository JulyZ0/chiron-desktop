import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileViewerRenderers } from '@file-viewer/vite-plugin'
import Components from 'unplugin-vue-components/vite'
import { AntDesignVueResolver } from 'unplugin-vue-components/resolvers'
import { compression, defineAlgorithm } from 'vite-plugin-compression2'
import AutoImport from 'unplugin-auto-import/vite'
import { visualizer } from 'rollup-plugin-visualizer'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = import.meta.dirname ?? path.dirname(fileURLToPath(import.meta.url))

// 开发代理指向的后端：默认与 run.py 的 Gateway 端口一致，可用环境变量覆盖。
const API_ORIGIN = process.env.CHIRON_DEV_API_ORIGIN || 'http://localhost:8080'
const WS_ORIGIN = API_ORIGIN.replace(/^http/, 'ws')

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  // 桌面壳（--mode desktop）用 loadFile 从 file:// 打开 dist：资源必须是相对路径，
  // 否则 /assets/* 会解析成 file:///X:/assets/*。Web 构建保持根绝对路径。
  base: mode === 'desktop' ? './' : '/',
  plugins: [
    vue(),
    fileViewerRenderers({ copyAssets: true }),
    AutoImport({
      imports: ['vue', 'vue-router'],
      dts: 'src/auto-imports.d.ts',
    }),
    // antd 按需引入：只打包模板中实际使用的 a-* 组件，显著削减首屏体积。
    // 组件样式由 antd v4 cssinjs 运行时注入，无需额外 style 导入。
    Components({
      resolvers: [AntDesignVueResolver({ importStyle: false })],
      dts: 'src/components.d.ts',
    }),
    // Gzip 压缩：减少传输体积
    compression({
      algorithms: [defineAlgorithm('gzip')],
      exclude: [/\.(br)$/, /\.(gz)$/],
      threshold: 1024,
      deleteOriginalAssets: false,
    }),
    // Brotli 压缩：更高压缩比
    compression({
      algorithms: [defineAlgorithm('brotliCompress')],
      exclude: [/\.(atlas)$/, /\.map$/, /\.json$/],
      threshold: 1024,
      deleteOriginalAssets: false,
    }),
    ...(process.env.ANALYZE ? [visualizer({
      open: true,
      gzipSize: true,
      brotliSize: true,
      filename: 'dist/stats.html',
    }) as any] : []),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
    // 强制 Vue 单实例：pnpm 为 ant-design-vue/@ant-design/icons-vue 解析到
    // node_modules/.pnpm 下的 vue@3.5.41，与 app 的 vue@3.5.39 不一致，
    // 导致 provide/inject 上下文断裂（prefixCls undefined）
    dedupe: ['vue'],
  },
  server: {
    // 代理后端 API：避免开发时 CORS 跨域问题（后端 CORS_ORIGINS 仅含 5173）
    proxy: {
      '/v1': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      '/events': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      '/ws': {
        target: WS_ORIGIN,
        ws: true,
      },
      '/metrics': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      // S 修复：/submit /cancel /media 为网关非 /v1 前缀路由，dev 亦需代理
      '/submit': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      '/cancel': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      '/media': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2020',
    minify: 'esbuild',
    sourcemap: false,
    cssCodeSplit: true,
    chunkSizeWarningLimit: 1000,
    // 启用 Treeshaking
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          // 按**包名**分组，不能用 id.includes('vue') 之类的子串匹配。
          //
          // 旧实现三处都错在子串：
          //   1) 项目自身路径 `…/chiron-desktop/frontend-vue/node_modules/…` 含 "vue"，
          //      而 Vite 传进来的是绝对路径 —— 于是**每一个**依赖都命中第一条规则；
          //   2) pnpm 把 peer 依赖编码进目录名（`@ant-design+icons-vue@7.0.1_vue@3.5.41_…`），
          //      任何依赖了 vue 的包路径里都带 "vue"；
          //   3) `id.includes('vue')` 同样会命中 `vue-echarts` / `@vue-flow/core`。
          // 结果：dist 里只剩一个 23MB 的 vendor-vue，另 6 个分组从未产出，
          // pdfjs / three / mermaid 全被塞进首屏包。
          //
          // 只取最后一个 node_modules/ 之后的部分，得到真实包名（含 scope）；
          // pnpm 的 `…/.pnpm/<pkg>@<ver>_<peers>/node_modules/<pkg>/…` 结构下，
          // 最后一处正好是包自身。
          //
          // 必须按**最后一个**切分：`.` 的 pnpm 布局里第一处 node_modules 之后
          // 跟的是 `.pnpm`，用 match() 取首个匹配会把每个依赖都算成包名 `.pnpm`，
          // 结果一条规则都不命中 —— 分组全部消失，全部回落到默认 chunk
          // （实测：dist 里 vendor-* 一个都没有，1088 个碎片 chunk）。
          const norm = String(id).replace(/\\/g, '/')
          const segments = norm.split('/node_modules/')
          const tail = segments[segments.length - 1]
          const m = tail.match(/^((?:@[^/]+\/)?[^/]+)\//)
          if (!m) return undefined
          const pkg = m[1]

          // 精确包名匹配：pkg === 'vue' 不会命中 'vue-echarts'；
          // '@vue/*' 是 vue 生态本体（runtime-core / shared / reactivity…）。
          if (pkg === 'vue' || pkg === 'pinia' || pkg.startsWith('@vue/')) return 'vendor-vue'
          if (pkg === 'ant-design-vue' || pkg.startsWith('@ant-design/')) return 'vendor-ui'
          if (pkg === 'echarts' || pkg === 'vue-echarts' || pkg === 'zrender') return 'vendor-charts'
          if (pkg === 'mermaid' || pkg.startsWith('@mermaid-js/')) return 'vendor-diagram'
          if (pkg === 'katex' || pkg === 'markdown-it') return 'vendor-markdown'
          if (pkg.startsWith('@vue-flow/')) return 'vendor-flow'
          if (pkg === 'three') return 'vendor-three'
          // 文件预览（pdf / cad / office）体积大且只有预览面板用到，单独成块，
          // 不该进首屏。
          if (pkg === 'pdfjs-dist' || pkg === 'pdf-lib' || pkg.startsWith('@file-viewer/')) {
            return 'vendor-fileviewer'
          }
          return undefined
        },
        // 优化代码分割和 tree-shaking
        hoistTransitiveImports: false,
      },
      // 强制 tree-shaking
      treeshake: {
        moduleSideEffects: 'no-external',
      },
    },
  },
}))
