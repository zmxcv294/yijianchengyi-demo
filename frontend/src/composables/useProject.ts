/**
 * 多页项目状态：加载、结构操作（串行）、批量生成与进度轮询。
 *
 * 两层版本号：
 * - 项目 revision 由结构操作（加/删/复制/排序）校验并 +1；
 * - 每页 task 的 revision 只由页面内容变化推进。
 *
 * 生成注意：
 * - 每次 render 都用新的 Idempotency-Key（重试必须换 key，否则拿到旧 job）；
 * - status 只返回精简字段，不能直接替换详情，需要按 id 合并；
 * - summary.rendering 统计所有 activeJob（含 analyze），进度里只看 render。
 */
import { onScopeDispose, ref } from 'vue'
import { ApiError, errorText } from '../api/client'
import { projects, staleTaskIds, stillRendering } from '../api/projects'
import type { Project, ProjectStatus, RenderResult } from '../api/projects'

const sleep = (ms: number) => new Promise<void>(resolve => window.setTimeout(resolve, ms))

export function useProject() {
  const project = ref<Project | null>(null)
  const loading = ref(false)
  const busy = ref(false)
  const polling = ref(false)
  const exporting = ref(false)
  const lastRender = ref<RenderResult | null>(null)
  const exportNote = ref('')
  /** 导出时后端报"预览过期"的页，用来在缩略图上打红标。 */
  const staleIds = ref<string[]>([])
  const error = ref('')
  const requestId = ref('')

  let chain: Promise<unknown> = Promise.resolve()
  let disposed = false
  onScopeDispose(() => { disposed = true })

  function showError(cause: unknown) {
    error.value = errorText(cause)
    requestId.value = cause instanceof ApiError ? cause.requestId : ''
    if (cause instanceof ApiError) {
      if (cause.code === 'PROJECT_REVISION_CONFLICT') error.value = '页面结构已在别处更新，正在刷新，请重试。'
      else if (cause.code === 'PAGE_LIMIT') error.value = '每个项目最多 10 页。'
      else if (cause.code === 'TASK_BUSY') error.value = '有页面正在生成，请等任务结束后再试。'
      else if (cause.code === 'EMPTY_PROJECT') error.value = '项目还没有页面。'
      else if (cause.code === 'VALIDATION_ERROR') error.value = cause.message
    }
  }

  async function load(identifier: string) {
    if (!identifier) return
    loading.value = true
    error.value = ''
    try {
      project.value = await projects.detail(identifier)
      // 生成中刷新页面时把轮询接上，否则进度会停住。
      if (project.value.pages.some(page => page.activeJob?.kind === 'render')) {
        void pollUntilDone()
      }
    } catch (cause) {
      project.value = null
      showError(cause)
    } finally {
      loading.value = false
    }
  }

  /** 结构操作后重新拉取；失败时保留现有数据，只提示错误。 */
  async function refresh() {
    if (!project.value) return
    error.value = ''
    try {
      project.value = await projects.detail(project.value.id)
    } catch (cause) {
      showError(cause)
    }
  }

  /** 串行执行，保证每次都用执行时刻最新的项目 revision。 */
  function enqueue<T>(task: () => Promise<T>): Promise<T> {
    const next = chain.then(task, task)
    chain = next.catch(() => undefined)
    return next
  }

  async function run(action: (identifier: string, revision: number) => Promise<Project>) {
    if (!project.value) return null
    busy.value = true
    error.value = ''
    requestId.value = ''
    try {
      const updated = await enqueue(() => {
        const current = project.value
        if (!current) throw new Error('项目尚未加载')
        return action(current.id, current.revision)
      })
      project.value = updated
      return updated
    } catch (cause) {
      showError(cause)
      if (cause instanceof ApiError && cause.code === 'PROJECT_REVISION_CONFLICT') void refresh()
      return null
    } finally {
      busy.value = false
    }
  }

  const addPage = (rawText: string) =>
    run((identifier, revision) => projects.addPage(identifier, revision, rawText))

  const copyPage = (sourceTaskId: string, sourceRevision: number) =>
    run((identifier, revision) => projects.copyPage(identifier, revision, sourceTaskId, sourceRevision))

  const deletePage = (taskId: string, pageRevision: number) =>
    run((identifier, revision) => projects.deletePage(identifier, revision, taskId, pageRevision))

  const reorder = (taskIds: string[]) =>
    run((identifier, revision) => projects.reorder(identifier, revision, taskIds))

  /** 把 status 的精简页合并进详情，保留 fields / displayName 等字段。 */
  function mergeStatus(status: ProjectStatus) {
    const current = project.value
    if (!current) return
    const byId = new Map(status.pages.map(page => [page.id, page]))
    project.value = {
      ...current,
      revision: status.revision,
      summary: status.summary,
      pages: current.pages.map(page => {
        const fresh = byId.get(page.id)
        if (!fresh) return page
        return {
          ...page,
          position: fresh.position,
          revision: fresh.revision,
          phase: fresh.phase,
          confirmedRevision: fresh.confirmedRevision,
          previewAssetId: fresh.previewAssetId,
          previewRevision: fresh.previewRevision,
          previewFresh: fresh.previewFresh,
          activeJob: fresh.activeJob,
          lastError: fresh.lastError,
        }
      }),
    }
  }

  /** 轮询到没有 render 任务为止；结束后拉一次完整详情。 */
  async function pollUntilDone() {
    if (!project.value) return
    polling.value = true
    const identifier = project.value.id
    try {
      while (!disposed) {
        const status = await projects.status(identifier)
        if (disposed) return
        mergeStatus(status)
        if (!stillRendering(status)) break
        await sleep(document.hidden ? 3000 : 1000)
      }
    } catch (cause) {
      if (!disposed) showError(cause)
    } finally {
      polling.value = false
    }
    if (!disposed) await refresh()
  }

  /** 为"已确认且预览过期"的页排队；已生成的会被后端跳过。 */
  async function renderAll() {
    const current = project.value
    if (!current || polling.value || busy.value) return
    if (!current.pages.length) { error.value = '项目还没有页面。'; return }
    error.value = ''
    requestId.value = ''
    lastRender.value = null
    busy.value = true
    try {
      const result = await projects.render(current.id)
      lastRender.value = result
      if (result.queued.length) {
        busy.value = false
        await pollUntilDone()
        return
      }
      await refresh()
    } catch (cause) {
      showError(cause)
    } finally {
      busy.value = false
    }
  }

  /** 导出多页 PDF；只在 summary.exportable 为真时可用。 */
  async function exportProject() {
    const current = project.value
    if (!current || exporting.value || !current.summary.exportable) return
    exporting.value = true
    error.value = ''
    requestId.value = ''
    exportNote.value = ''
    staleIds.value = []
    try {
      const { pages } = await projects.exportPdf(current.id, current.revision, current.title)
      exportNote.value = `已导出 ${pages || current.pages.length} 页 PDF`
    } catch (cause) {
      showError(cause)
      staleIds.value = staleTaskIds(cause)
      if (staleIds.value.length) {
        error.value = `有 ${staleIds.value.length} 页预览已过期，请先生成这些页面。`
      }
      if (cause instanceof ApiError && cause.code === 'PROJECT_REVISION_CONFLICT') void refresh()
    } finally {
      exporting.value = false
    }
  }

  return {
    project, loading, busy, polling, exporting, lastRender, exportNote, staleIds, error, requestId,
    load, refresh, addPage, copyPage, deletePage, reorder, renderAll, exportProject,
  }
}
