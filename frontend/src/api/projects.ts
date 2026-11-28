/**
 * 多页项目 API（/api/v2/projects）。
 *
 * 契约来源：backend/app/api/projects_v2.py + backend/app/services/projects.py
 * （patch 0001-feat-PDF，2026-11-25）。
 *
 * 注意：
 * - 这些类型手写在这里，不要改 src/types/api.d.ts（那是 v1 生成物）。
 * - 所有写操作都返回**完整项目详情**，所以每次操作后不需要再 GET 一次。
 * - 项目 revision 与页面(task) revision 是两套版本号，别混用。
 */
import { request, ApiError } from './client'
import type { Job, Task } from './types'
import type { TaskView } from './extensions'

export type Scenario = 'poster' | 'article_image'

/** 后端 MAX_PAGES；POST /pages 超出返回 409 PAGE_LIMIT。 */
export const MAX_PAGES = 10

/** 项目详情里的一页 = 完整 task 视图 + 位置 + 预览新鲜度。 */
export interface ProjectPage extends TaskView {
  /** 0 起始，不是 1。 */
  position: number
  /** previewAssetId 存在且 previewRevision === revision。 */
  previewFresh: boolean
}

export interface ProjectSummary {
  total: number
  fresh: number
  /** 注意：统计的是 activeJob !== null 的页数，**包含 analyze 任务**，不只是 render。 */
  rendering: number
  /** phase === 'failed' 且没有 activeJob。 */
  failed: number
  /** confirmedRevision !== revision 且没有 activeJob；新建草稿页也会计入。 */
  needsConfirmation: number
  /** pages 非空且每一页都 previewFresh。 */
  exportable: boolean
}

export interface Project {
  id: string
  scenario: Scenario
  title: string
  revision: number
  createdAt: string
  updatedAt: string
  pages: ProjectPage[]
  summary: ProjectSummary
}

export interface ProjectListItem {
  id: string
  scenario: Scenario
  title: string
  revision: number
  pageCount: number
  coverAssetId: string | null
  createdAt: string
  updatedAt: string
}

/** render 的 202 响应。 */
export interface RenderResult {
  projectId: string
  /** 每个元素带 taskId 和完整 job。 */
  queued: { taskId: string; job: Job }[]
  skipped: { taskId: string; reason: 'PREVIEW_FRESH' | 'CONFIRMATION_REQUIRED' }[]
  /** code 例如 QUEUE_FULL / TASK_BUSY。 */
  failed: { taskId: string; code: string; message: string }[]
}

/** status 的精简页。⚠️ 主键叫 id，不叫 taskId（与 render 不一致）。 */
export interface ProjectStatusPage {
  id: string
  position: number
  revision: number
  phase: Task['phase']
  confirmedRevision: number | null
  previewAssetId: string | null
  previewRevision: number | null
  previewFresh: boolean
  activeJob: Job | null
  lastError: Task['lastError']
}

export interface ProjectStatus {
  id: string
  revision: number
  pages: ProjectStatusPage[]
  summary: ProjectSummary
}

const base = (id: string) => `/api/v2/projects/${encodeURIComponent(id)}`
const json = (body: unknown, method = 'POST') => ({ method, body: JSON.stringify(body) })

export const projects = {
  // GET /api/v2/projects —— 最多 50 条，按 updated_at 倒序（只有结构变化才更新 updated_at）
  list: () => request<{ items: ProjectListItem[] }>('/api/v2/projects'),

  // POST /api/v2/projects —— 201，返回 pages: [] 的完整项目；title 1..80，scenario 枚举
  create: (scenario: Scenario, title: string) =>
    request<Project>('/api/v2/projects', json({ scenario, title })),

  detail: (id: string) => request<Project>(base(id)),

  // PATCH —— 200，返回完整项目（revision +1）
  rename: (id: string, expectedRevision: number, title: string) =>
    request<Project>(base(id), json({ expectedRevision, title }, 'PATCH')),

  // DELETE —— 204，无响应体。有页面在生成时 409 TASK_BUSY，details.taskIds 列出忙的页
  remove: (id: string, expectedRevision: number) =>
    request<void>(`${base(id)}?expectedRevision=${expectedRevision}`, { method: 'DELETE' }),

  /**
   * POST /{id}/pages —— 201，返回完整项目（revision +1）。
   * 空白页：rawText 必填（strip 后非空，否则 422 VALIDATION_ERROR），追加到末尾。
   * 复制页：sourceTaskId + sourceRevision，插在源页之后，副本 phase=draft。
   * 达到 10 页返回 409 PAGE_LIMIT（details.maxPages）。
   * ⚠️ 响应里没有"新页 id"字段：空白页取最后一个，复制页取源页后一位。
   */
  addPage: (id: string, expectedRevision: number, rawText: string, style?: string | null) =>
    request<Project>(`${base(id)}/pages`, json({
      expectedRevision, rawText, ...(style ? { style } : {}),
    })),

  copyPage: (id: string, expectedRevision: number, sourceTaskId: string, sourceRevision: number) =>
    request<Project>(`${base(id)}/pages`, json({ expectedRevision, sourceTaskId, sourceRevision })),

  // DELETE /{id}/pages/{taskId} —— 200，返回完整项目（位置重排 + revision +1）
  deletePage: (id: string, expectedRevision: number, taskId: string, pageRevision: number) =>
    request<Project>(
      `${base(id)}/pages/${encodeURIComponent(taskId)}?expectedRevision=${expectedRevision}&pageRevision=${pageRevision}`,
      { method: 'DELETE' },
    ),

  /**
   * PUT /{id}/order —— 200，返回完整项目。
   * taskIds 必须恰好是当前全部页面（集合相等且无重复），否则 422 VALIDATION_ERROR，
   * details.currentTaskIds 给出服务端当前顺序。
   * ⚠️ 顺序没有变化时后端不 bump revision（返回原 revision）。
   */
  reorder: (id: string, expectedRevision: number, taskIds: string[]) =>
    request<Project>(`${base(id)}/order`, json({ expectedRevision, taskIds }, 'PUT')),

  /**
   * POST /{id}/render —— 202。
   * 只为"已确认且预览过期"的页排队；空项目 409 EMPTY_PROJECT。
   * 每页的幂等键由 `key:taskId` 派生，所以**重试必须换新的 key**。
   */
  render: (id: string) =>
    request<RenderResult>(`${base(id)}/render`, {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    }),

  status: (id: string, signal?: AbortSignal) =>
    request<ProjectStatus>(`${base(id)}/status`, { signal }),

  /**
   * GET /{id}/export?expectedRevision= —— 200 application/pdf。
   * 响应头 X-Export-Pages 是页数，X-Export-Content: raster-preview。
   * 409 EMPTY_PROJECT / 409 PREVIEW_STALE(details.taskIds) /
   * 409 PROJECT_REVISION_CONFLICT(details.currentRevision) / 503 RENDER_FAILED。
   */
  async exportPdf(id: string, expectedRevision: number, title: string) {
    let response: Response
    try {
      response = await fetch(`${base(id)}/export?expectedRevision=${expectedRevision}`, {
        credentials: 'same-origin',
      })
    } catch {
      throw new ApiError(0, 'NETWORK_ERROR', '下载连接中断，请检查网络后重试；项目已保留。')
    }
    if (!response.ok) {
      const body = await response.json().catch(() => null) as
        { error?: { code?: string; message?: string; details?: Record<string, unknown> }; requestId?: string } | null
      throw new ApiError(
        response.status,
        body?.error?.code || 'EXPORT_FAILED',
        body?.error?.message || '导出未能完成，请重试。',
        body?.error?.details,
        body?.requestId,
      )
    }
    const pages = Number(response.headers.get('X-Export-Pages') || 0)
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = `${title.replace(/[\\/:*?"<>|]/g, '').slice(0, 60) || '多页作品'}${pages ? `-${pages}页` : ''}.pdf`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    return { pages }
  },
}

// ---------------------------------------------------------------- 纯函数助手

/**
 * 从 addPage/copyPage 的返回值里找出新页 id。
 * 后端不返回新页 id：空白页追加在末尾，复制页紧跟源页。
 */
export function addedPageId(project: Project, sourceTaskId?: string): string | null {
  const pages = project.pages
  if (!pages.length) return null
  if (!sourceTaskId) return pages[pages.length - 1].id
  const index = pages.findIndex(page => page.id === sourceTaskId)
  return index >= 0 && index + 1 < pages.length ? pages[index + 1].id : null
}

/** 当前顺序的 id 列表，用于 PUT /order。 */
export function orderIds(pages: ProjectPage[]): string[] {
  return [...pages].sort((a, b) => a.position - b.position).map(page => page.id)
}

/** 本地移动一页（纯函数，用于拖拽/上移下移的乐观更新）。 */
export function movePage<T>(items: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) return items
  const next = [...items]
  const [moved] = next.splice(from, 1)
  next.splice(to, 0, moved)
  return next
}

/** 409 PREVIEW_STALE 里过期的页 id。 */
export function staleTaskIds(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.code !== 'PREVIEW_STALE') return []
  const ids = (error.details as { taskIds?: unknown })?.taskIds
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : []
}

/** 409 TASK_BUSY 里正在生成的页 id（删除项目/页面时用）。 */
export function busyTaskIds(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.code !== 'TASK_BUSY') return []
  const ids = (error.details as { taskIds?: unknown })?.taskIds
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : []
}

/** 真正在生成（render）的页；status 里的 activeJob 也可能是 analyze。 */
export function renderingPages(status: ProjectStatus): ProjectStatusPage[] {
  return status.pages.filter(page => page.activeJob?.kind === 'render')
}

/**
 * 轮询是否应该继续。
 * 只按 render 任务判断：analyze 也会让 activeJob 非空，但那时不该显示"生成中"。
 */
export function stillRendering(status: ProjectStatus): boolean {
  return renderingPages(status).length > 0
}

/**
 * 轮询结束但仍有页不新鲜、且没有失败页 —— 说明生成期间页面被改过，
 * 结果被判 superseded，后端不会报错。这种情况要给用户一句明确提示。
 */
export function needsRegenerationHint(status: ProjectStatus): boolean {
  const { total, fresh, failed } = status.summary
  return total > 0 && fresh < total && failed === 0 && !stillRendering(status)
}
