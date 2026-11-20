import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import path from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, 'src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['src/test-setup.ts'],
    include: ['src/**/*.{test,spec}.ts'],
    exclude: ['node_modules/**', 'dist/**'],

    // ── 稳定性（P2-13）────────────────────────────────────────────────────
    // 背景：全量用例在本机高负载（并行构建 / 其他 worktree 同时跑测试）下偶发
    // 「5s 超时」抖动，而单跑该用例必绿 —— 这是**机器争用**，不是断言失效。
    // 判定口径：调大超时只放宽「等待上限」，不改变任何断言语义；真正的死循环
    // 仍会在 15s 后被 abort（不会变成假绿）。
    //
    // 15s 的取值：默认 5s 的 3 倍，足以覆盖 jsdom 下挂载 + 路由/懒加载 chunk 的
    // 冷启动毛刺；再往大（30s+）只会让真正的挂起多拖一倍时间才报错。
    testTimeout: 15_000,
    // hook 比用例更贵（beforeAll 里常有 mock 模块重置 / 组件挂载），给到 20s。
    hookTimeout: 20_000,

    // 每条用例之间重置模块图与全局状态。jsdom 环境下多个 spec 共享 window 时，
    // 残留的 mock / window 属性会让「单跑绿、全量红」，isolate 是消除这类串扰
    // 的前提；代价是每条文件多一次环境重建（可接受，远小于排障成本）。
    isolate: true,

    // 用进程池而非线程池：jsdom + Vue 组件测试对 `process.env` / 原生 addon
    // 的使用（electron 相关 polyfill、path）在线程间会互相污染，且共享 heap 时
    // 偶发 OOM 更容易拖垮同池的其它用例。进程池的隔离性换来的是**确定性的失败**，
    // 而不是随机的超时。Vitest 4 默认已是 forks，这里显式写出以锁定该选择。
    pool: 'forks',

    // 并发保留（不关），但限制 worker 上限：全量 157 个 spec 在 8+ 核机器上
    // 默认可能拉起接近核数的 fork，每个 fork 都带一份 jsdom，内存与 IO 争用
    // 正是 5s 超时的来源。取 50% 让负载有上限，同时仍比串行快一个量级。
    maxWorkers: '50%',
  },
})
