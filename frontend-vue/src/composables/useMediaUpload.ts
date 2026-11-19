import { ref, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import type { UploadFile, UploadProps } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { createChunkUpload } from '../utils/uploader'

/** Upload 的 customRequest 参数：从 antd 的 props 推导，避免手抄内部类型名 */
export type UploadRequestOptions = Parameters<NonNullable<UploadProps['customRequest']>>[0]

interface UploadState {
  progress: number
  status: 'uploading' | 'success' | 'error'
  error?: string
  /**
   * 展示用的文件名。
   *
   * 此前模板从 key 里 `split('-')[0]` 反推文件名 —— 文件名本身含 `-` 时会被截断，
   * 而拖拽上传的 key 还带上了目录 id 前缀，反推就更不成立了。名字在这里存一份，
   * key 就能自由编码（目录、大小、时间）。
   */
  name?: string
}

/**
 * 媒体库的批量上传（拖拽 + 多文件 + 断点续传）。
 *
 * 从 `MediaView.vue` 抽出，逻辑逐字未改。`parentId` 与 `onUploaded` 由调用方注入：
 * 前者是当前的目录上下文（上传到哪个文件夹），后者用于上传结束后重拉列表
 * —— 两者都是视图的职责，不属于上传本身。
 */
export function useMediaUpload(opts: {
  parentId: Ref<string>
  onUploaded: () => void | Promise<void>
}) {
  const { t: tr } = useI18n()

  const uploadFileList = ref<UploadFile[]>([])
  const uploadingFiles = ref<Map<string, UploadState>>(new Map())

  /** 上传失败原因：上传器抛的是 Error（message 即后端说明），取不到时用调用方兜底 */
  function uploadErrorText(err: unknown, fallback: string): string {
    return err instanceof Error && err.message ? err.message : fallback
  }

  /** antd 的 onError 只接受 Error/UploadRequestError，而 catch 到的是 unknown */
  function toUploadError(err: unknown): Error {
    if (err instanceof Error) return err
    return new Error(typeof err === 'string' ? err : String(err))
  }

  /**
   * 上传单个文件，并在 `uploadingFiles` 里维护进度。
   *
   * `notify` 默认为 true（上传弹窗里逐文件提示是合适的）；批量拖拽必须传 false
   * —— 拖进来 200 个文件时会弹出 200 条 message，把界面糊死，而用户真正需要的是
   * 最后那句汇总。进度仍然逐个记录，所以弹窗里看到的进度一样。
   */
  function uploadOne(
    file: File,
    extra: {
      parentId?: string
      notify?: boolean
      /** 每完成一个文件就重拉列表。批量场景传 false，由调用方最后统一刷一次。 */
      refresh?: boolean
      onProgress?: (pct: number) => void
    } = {},
  ): Promise<void> {
    const parentId = extra.parentId ?? opts.parentId.value
    const notify = extra.notify !== false
    const refresh = extra.refresh !== false
    // key 里带母目录：同一个文件拖到不同目录是两次独立上传（也是两处不同的资产）。
    const fileId = `${parentId || 'root'}:${file.name}:${file.size}:${file.lastModified}`
    uploadingFiles.value.set(fileId, { progress: 0, status: 'uploading', name: file.name })

    let lastError: unknown = null
    const markError = (fallback: string) =>
      uploadingFiles.value.set(fileId, {
        progress: 0,
        status: 'error',
        name: file.name,
        error: uploadErrorText(lastError, fallback),
      })

    return createChunkUpload(file, { purpose: 'media', parentId })
      .then((handle) => {
        handle.onProgress((p) => {
          extra.onProgress?.(p)
          const current = uploadingFiles.value.get(fileId)
          if (current) uploadingFiles.value.set(fileId, { ...current, progress: p })
        })
        return handle.done
          .then(() => {
            uploadingFiles.value.set(fileId, {
              progress: 100,
              status: 'success',
              name: file.name,
            })
            if (notify) message.success(tr('common.uploaded_name', { name: file.name }))
            // 3秒后移除成功记录
            setTimeout(() => uploadingFiles.value.delete(fileId), 3000)
          })
          .catch((err: unknown) => {
            lastError = err
            markError(tr('errors.unknown_error'))
            if (notify) {
              message.error(tr('errors.upload_failed_name_reason', {
                name: file.name,
                reason: uploadErrorText(err, tr('errors.unknown_error')),
              }))
            }
            // 继续往外抛：调用方（拖拽批量上传）需要按文件统计失败数，
            // 而不是去翻 Map 猜哪几个失败了。
            throw err
          })
          .finally(() => { if (refresh) opts.onUploaded() })
      })
      .catch((err: unknown) => {
        lastError = err
        // 初始化阶段失败时状态仍是 uploading；done 阶段的失败已在上面的
        // catch 里写成 error（带真实原因），这里不能覆盖成"初始化失败"。
        if (uploadingFiles.value.get(fileId)?.status !== 'error') {
          markError(tr('errors.initialization_failed'))
          if (notify) {
            message.error(tr('errors.upload_failed_name_init_failed', { name: file.name }))
          }
        }
        throw err
      })
  }

  function handleUploadRequest(options: UploadRequestOptions) {
    const { file, onProgress, onSuccess, onError } = options
    // antd 的 file 允许多态（string/Blob，用于受控 fileList）；本视图只接真实上传文件
    if (!(file instanceof File)) {
      onError?.(new Error('unsupported upload payload'))
      return
    }
    uploadOne(file, { onProgress: (pct) => onProgress?.({ percent: pct }) })
      .then(() => onSuccess?.(null))
      .catch((err: unknown) => onError?.(toUploadError(err)))
  }

  // 暂停所有上传
  function pauseAllUploads() {
    // 注意: 这里需要修改 uploader.ts 暴露暂停接口
    message.info(tr('common.pause_feature_in_development'))
  }

  // 重试失败的上传
  async function retryFailedUploads() {
    const failedEntries = Array.from(uploadingFiles.value.entries())
      .filter(([, state]) => state.status === 'error')

    if (failedEntries.length === 0) {
      message.info(tr('errors.no_failed_upload_tasks'))
      return
    }

    message.info(tr('errors.retrying_n_failed_uploads', { n: failedEntries.length }))

    // 指数退避重试函数
    const uploadWithRetry = async (fileId: string, maxRetries: number = 3) => {
      for (let attempt = 1; attempt <= maxRetries; attempt++) {
        try {
          // 更新状态为重试中
          uploadingFiles.value.set(fileId, {
            progress: 0,
            status: 'uploading',
            error: undefined,
          })

          // 从 fileId 中提取文件信息（简化处理，实际应该存储文件引用）
          // 这里假设用户会重新选择文件进行重试
          message.warning(tr('media.please_re_select_the_file_to_retry_id', { id: fileId }))
          return true
        } catch {
          // 静默失败，重试逻辑由调用方处理

          if (attempt < maxRetries) {
            // 指数退避：1s, 2s, 4s...
            const delay = Math.pow(2, attempt - 1) * 1000
            await new Promise(resolve => setTimeout(resolve, delay))
          }
        }
      }

      // 所有重试都失败
      uploadingFiles.value.set(fileId, {
        progress: 0,
        status: 'error',
        error: tr('errors.still_failed_after_n_retries', { n: maxRetries }),
      })
      return false
    }

    // 并行重试所有失败的文件
    const results = await Promise.all(
      failedEntries.map(([fileId]) => uploadWithRetry(fileId)),
    )

    const successCount = results.filter(r => r).length
    const failCount = results.length - successCount

    if (successCount > 0) {
      message.success(tr('common.retried_n_uploads_successfully', { n: successCount }))
    }
    if (failCount > 0) {
      message.error(tr('errors.n_uploads_failed_after_retry_please_re_upload_manually', { n: failCount }))
    }
  }

  // 清除已完成的上传记录
  function clearCompletedUploads() {
    for (const [key, state] of uploadingFiles.value.entries()) {
      if (state.status === 'success') {
        uploadingFiles.value.delete(key)
      }
    }
    message.success(tr('common.completed_upload_records_cleared'))
  }

  return {
    uploadFileList,
    uploadingFiles,
    handleUploadRequest,
    uploadOne,
    pauseAllUploads,
    retryFailedUploads,
    clearCompletedUploads,
  }
}
