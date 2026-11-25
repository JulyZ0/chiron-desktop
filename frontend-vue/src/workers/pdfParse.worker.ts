/**
 * G21 —— PDF 预解析 Worker（模块 Worker，由 `src/utils/pdfWorker.ts` 创建）。
 *
 * 这里跑的是「页数 / 逐页尺寸」这类**纯字节计算**：几十 MB 的 PDF 在主线程上做一次
 * 全量扫描会把滚动和输入卡住；挪到这条线程上，主线程只负责 `await` 一个 Promise。
 *
 * 协议（主线程 ↔ Worker，全部结构化克隆 + ArrayBuffer 转移）：
 *   请求 `{ id, bytes }`  —— bytes 的所有权转移给 Worker，主线程不留副本；
 *   回包 `{ id, ok: true, summary }` 或 `{ id, ok: false, reason }`。
 *
 * 为什么不引 `/// <reference lib="webworker" />`：本仓 tsconfig 用的是 `lib.dom`
 * （见 tsconfig.app.json），再引入 webworker 全局库会和 dom 的 `self`/`fetch` 等声明
 * 冲突。这里用一个结构化小接口接住 Worker 全局的两个方法，既拿到类型又不动编译配置。
 */

import { summarizePdf } from './pdfStructure'

export interface PdfWorkerRequest {
  id: number
  bytes: ArrayBuffer
}

export type PdfWorkerResponse =
  | { id: number; ok: true; summary: { pages: number; sizes: Array<[number, number]> } }
  | { id: number; ok: false; reason: string }

interface WorkerScope {
  addEventListener(type: 'message', listener: (event: MessageEvent<PdfWorkerRequest>) => void): void
  postMessage(message: PdfWorkerResponse): void
}

const scope = globalThis as unknown as WorkerScope

scope.addEventListener('message', (event) => {
  const request = event.data
  const id = typeof request?.id === 'number' ? request.id : 0
  try {
    const result = summarizePdf(new Uint8Array(request.bytes))
    if (result.ok) {
      scope.postMessage({ id, ok: true, summary: result.summary })
    } else {
      scope.postMessage({ id, ok: false, reason: result.reason })
    }
  } catch (error) {
    // Worker 内部异常也必须回包：否则主线程只能等超时，白白多等几秒才降级。
    scope.postMessage({ id, ok: false, reason: error instanceof Error ? error.message : 'worker-crashed' })
  }
})
