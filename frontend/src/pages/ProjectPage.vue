<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { MAX_PAGES, addedPageId, movePage } from '../api/projects'
import type { ProjectPage } from '../api/projects'
import { useProject } from '../composables/useProject'

const route = useRoute()
const { project, loading, busy, polling, exporting, lastRender, exportNote, staleIds, error, requestId, load, refresh, addPage, copyPage, deletePage, reorder, renderAll, exportProject } = useProject()

const projectId = computed(() => (typeof route.params.projectId === 'string' ? route.params.projectId : ''))
const scenarioLabel = computed(() => (project.value?.scenario === 'article_image' ? '公众号配图' : '竖版海报'))
const pageName = (page: ProjectPage) => page.displayName || page.fields.title || '未命名页'

const selectedId = ref<string | null>(null)
const broken = ref<string[]>([])

const addOpen = ref(false)
const addText = ref('')
const removing = ref<ProjectPage | null>(null)

const full = computed(() => (project.value?.pages.length ?? 0) >= MAX_PAGES)

/** 只有"预览是当前版本"时才显示图片，否则给占位。 */
const previewUrl = (page: ProjectPage) => {
  if (!page.previewAssetId || !page.previewFresh || broken.value.includes(page.id)) return ''
  return `/api/assets/${page.previewAssetId}/file?v=${page.revision}`
}
const markBroken = (page: ProjectPage) => {
  if (!broken.value.includes(page.id)) broken.value.push(page.id)
}
/** 有预览但已过期：内容改过，需要重新生成。 */
const stale = (page: ProjectPage) => !!page.previewAssetId && !page.previewFresh

const selected = computed(() => project.value?.pages.find(page => page.id === selectedId.value) || null)
const selectedIndex = computed(() => project.value?.pages.findIndex(page => page.id === selectedId.value) ?? -1)

/** 生成进度：只统计 render 任务，避开 analyze 干扰。 */
const renderingCount = computed(() =>
  (project.value?.pages || []).filter(page => page.activeJob?.kind === 'render').length)
const percent = computed(() => {
  const summary = project.value?.summary
  if (!summary || !summary.total) return 0
  return Math.round((summary.fresh / summary.total) * 100)
})

/** 缩略图状态点：生成中 / 失败 / 待确认 / 已是最新。 */
const pageState = (page: ProjectPage) => {
  if (page.activeJob) return 'busy'
  if (page.phase === 'failed') return 'failed'
  if (page.confirmedRevision !== page.revision) return 'confirm'
  if (page.previewFresh) return 'fresh'
  return ''
}

const renderNote = computed(() => {
  const result = lastRender.value
  if (!result) return ''
  const parts: string[] = []
  if (result.queued.length) parts.push(`已排队 ${result.queued.length} 页`)
  const fresh = result.skipped.filter(item => item.reason === 'PREVIEW_FRESH').length
  const confirm = result.skipped.filter(item => item.reason === 'CONFIRMATION_REQUIRED').length
  if (fresh) parts.push(`${fresh} 页已是最新`)
  if (confirm) parts.push(`${confirm} 页未确认`)
  if (result.failed.length) parts.push(`${result.failed.length} 页未排上队`)
  return parts.join('，')
})

/** 导出按钮不可用时的原因，直接告诉用户缺什么。 */
const exportBlockReason = computed(() => {
  const summary = project.value?.summary
  if (!summary || !summary.total) return '项目还没有页面'
  if (summary.exportable) return ''
  if (summary.needsConfirmation) return `还有 ${summary.needsConfirmation} 页没确认`
  if (summary.rendering) return '有页面正在生成'
  return `有 ${summary.total - summary.fresh} 页预览过期，需要重新生成`
})

watch(project, value => {
  if (!value) { selectedId.value = null; return }
  if (!value.pages.some(page => page.id === selectedId.value)) {
    selectedId.value = value.pages[0]?.id ?? null
  }
})

function openAdd() {
  addText.value = selected.value?.rawText || ''
  addOpen.value = true
}

async function submitAdd() {
  const text = addText.value.trim()
  if (!text || busy.value) return
  const updated = await addPage(text)
  if (!updated) return
  addOpen.value = false
  addText.value = ''
  selectedId.value = addedPageId(updated) ?? selectedId.value
}

async function duplicate(page: ProjectPage) {
  if (busy.value || full.value) return
  const updated = await copyPage(page.id, page.revision)
  if (updated) selectedId.value = addedPageId(updated, page.id) ?? selectedId.value
}

async function confirmRemove() {
  const page = removing.value
  if (!page || busy.value) return
  const updated = await deletePage(page.id, page.revision)
  if (!updated) return
  removing.value = null
  if (selectedId.value === page.id) selectedId.value = updated.pages[0]?.id ?? null
}

async function move(from: number, delta: number) {
  const current = project.value
  if (!current || busy.value) return
  const to = from + delta
  if (to < 0 || to >= current.pages.length) return
  // 先本地乐观排序，再提交完整顺序；失败时以服务端为准回滚。
  const pages = movePage([...current.pages], from, to).map((page, index) => ({ ...page, position: index }))
  const ids = pages.map(page => page.id)
  project.value = { ...current, pages }
  // 提交失败时不能把错误的本地顺序留在界面上，以服务端为准回滚。
  if (!(await reorder(ids))) await refresh()
}

watch(projectId, id => { if (id) void load(id) }, { immediate: true })
</script>
<template>
  <div class="page project-page">
    <div class="project-head">
      <div>
        <RouterLink to="/history" class="back-link"><AppIcon name="back" :size="16" />返回我的作品</RouterLink>
        <h1>{{ project?.title || '多页项目' }}<span v-if="project" class="project-badge">{{ scenarioLabel }}</span></h1>
        <p class="muted">
          <template v-if="project">{{ project.pages.length }} / {{ MAX_PAGES }} 页 · 已生成 {{ project.summary.fresh }} 页 · {{ busy ? '正在保存结构…' : '结构已保存' }}</template>
          <template v-else>正在读取项目…</template>
        </p>
      </div>
      <div class="project-actions">
        <button class="button primary compact" type="button" :disabled="busy || polling || !project?.pages.length" @click="renderAll">
          <AppIcon :name="polling ? 'refresh' : 'spark'" :size="16" />{{ polling ? '正在生成…' : busy ? '正在排队…' : '生成全部页面' }}
        </button>
        <button
          class="button outline compact"
          type="button"
          :disabled="!project?.summary.exportable || exporting || polling"
          :title="exportBlockReason || '导出多页 PDF'"
          @click="exportProject"
        ><AppIcon name="download" :size="16" />{{ exporting ? '正在导出…' : '导出 PDF' }}</button>
      </div>
    </div>

    <div v-if="project && project.pages.length" class="project-progress">
      <span class="progress-track"><span :style="{ width: percent + '%' }" /></span>
      <span class="project-progress-text">
        已生成 {{ project.summary.fresh }} / 共 {{ project.summary.total }}
        <template v-if="renderingCount"> · 生成中 {{ renderingCount }}</template>
        <template v-if="project.summary.failed"> · 失败 {{ project.summary.failed }}</template>
        <template v-if="project.summary.needsConfirmation"> · 待确认 {{ project.summary.needsConfirmation }}</template>
      </span>
    </div>
    <p v-if="renderNote" class="inline-notice">{{ renderNote }}</p>
    <p v-if="exportNote" class="success-notice">{{ exportNote }}</p>
    <p v-if="exportBlockReason && !project?.summary.needsConfirmation" class="field-help">导出 PDF 前需要：{{ exportBlockReason }}。</p>
    <p v-if="project?.summary.needsConfirmation && !polling" class="inline-notice">
      还有 {{ project.summary.needsConfirmation }} 页没确认：点进对应页面，完成事实确认后才能生成。
    </p>
    <p v-if="project?.summary.failed && !polling" class="inline-notice">
      有 {{ project.summary.failed }} 页生成失败：再点一次「生成全部页面」会重试这些页，已生成的会自动跳过。
    </p>

    <div v-if="error" class="error-card" role="alert">
      <AppIcon name="warning" />
      <div>
        <strong>{{ error }}</strong>
        <small v-if="requestId">请求编号：{{ requestId }}</small>
        <button class="text-button" @click="load(projectId)">重新加载</button>
      </div>
    </div>

    <div v-if="loading" class="empty-state"><span class="spinner" /><p>正在读取项目…</p></div>

    <div v-else-if="project" class="project-body">
      <aside class="project-rail" aria-label="页面列表">
        <div class="project-rail-head"><strong>页面</strong><span>{{ project.pages.length }} / {{ MAX_PAGES }}</span></div>
        <ol class="project-page-list">
          <li v-for="(page, index) in project.pages" :key="page.id" class="project-page-row">
            <button
              class="project-page-item"
              :class="{ selected: page.id === selectedId }"
              type="button"
              :aria-current="page.id === selectedId ? 'true' : undefined"
              @click="selectedId = page.id"
            >
              <span class="project-page-no">{{ index + 1 }}</span>
              <span class="project-page-thumb">
                <img v-if="previewUrl(page)" :src="previewUrl(page)" :alt="`${pageName(page)}缩略图`" loading="lazy" decoding="async" @error="markBroken(page)" />
                <AppIcon v-else name="file" :size="18" />
              </span>
              <span class="project-page-name">{{ pageName(page) }}</span>
              <span v-if="pageState(page)" class="page-dot" :class="pageState(page)" :title="pageState(page) === 'busy' ? '正在生成' : pageState(page) === 'failed' ? '生成失败' : pageState(page) === 'confirm' ? '待确认' : '已生成'" />
              <span v-if="staleIds.includes(page.id)" class="project-stale" title="导出时发现这一页预览过期">过期</span>
              <span v-else-if="stale(page)" class="project-stale" title="内容已修改，需要重新生成">旧</span>
            </button>
            <div v-if="page.id === selectedId" class="project-page-actions">
              <button class="text-button" type="button" :disabled="busy || index === 0" @click="move(index, -1)">上移</button>
              <button class="text-button" type="button" :disabled="busy || index === project.pages.length - 1" @click="move(index, 1)">下移</button>
              <button class="text-button" type="button" :disabled="busy || full" @click="duplicate(page)">复制</button>
              <button class="text-button danger-text" type="button" :disabled="busy" @click="removing = page">删除</button>
            </div>
          </li>
        </ol>
        <p v-if="!project.pages.length" class="field-help">还没有页面。</p>
        <button class="button outline compact project-add" type="button" :disabled="busy || full" @click="openAdd">
          <AppIcon name="plus" :size="16" />{{ full ? '已达 10 页上限' : '添加页' }}
        </button>
      </aside>

      <section class="project-main">
        <div v-if="!project.pages.length" class="empty-state">
          <span class="empty-icon"><AppIcon name="image" :size="30" /></span>
          <h3>这个项目还没有页面</h3>
          <p>点击左侧「添加页」写下第一页的内容。</p>
          <button class="button primary" type="button" :disabled="busy" @click="openAdd"><AppIcon name="plus" :size="16" />添加第一页</button>
        </div>
        <div v-else-if="selected" class="project-preview">
          <div class="project-preview-stage">
            <img v-if="previewUrl(selected)" :src="previewUrl(selected)" :alt="`${pageName(selected)}预览`" @error="markBroken(selected)" />
            <div v-else class="project-preview-empty">
              <AppIcon name="image" :size="34" />
              <p>{{ stale(selected) ? '这一页改过内容，需要重新生成预览' : '这一页还没有生成预览' }}</p>
            </div>
          </div>
          <div class="project-preview-foot">
            <p class="field-help">{{ pageName(selected) }} · 第 {{ selectedIndex + 1 }} 页 · 版本 {{ selected.revision }}</p>
            <RouterLink class="button primary compact" :to="`/projects/${projectId}/pages/${selected.id}`">编辑这一页<AppIcon name="arrow" :size="16" /></RouterLink>
          </div>
        </div>
      </section>
    </div>

    <div v-if="addOpen" class="management-modal" role="dialog" aria-modal="true" aria-label="添加页面">
      <form class="management-dialog" @submit.prevent="submitAdd">
        <h2>添加一页</h2>
        <p class="field-help">写下这一页要表达的文案，添加后可以选模板、分析和生成。</p>
        <label class="field-label" for="page-text">这一页的内容</label>
        <textarea id="page-text" v-model="addText" rows="6" maxlength="10000" placeholder="例如：标题：秋日读书会。时间：11月20日 14:00。地点：图书馆一楼交流区。" autofocus />
        <p class="field-help">已填 {{ Array.from(addText).length }} / 10000 字</p>
        <div class="inline-actions">
          <button class="button primary" :disabled="busy || !addText.trim()">{{ busy ? '正在添加…' : '添加这一页' }}</button>
          <button type="button" class="button outline" :disabled="busy" @click="addOpen = false">取消</button>
        </div>
      </form>
    </div>

    <div v-if="removing" class="management-modal" role="dialog" aria-modal="true" aria-label="确认删除页面">
      <div class="management-dialog">
        <h2>删除这一页？</h2>
        <p>将删除「{{ pageName(removing) }}」的内容、排版和预览，后面的页会自动补位。此操作无法撤销。</p>
        <div class="inline-actions">
          <button class="button primary danger-button" :disabled="busy" @click="confirmRemove">{{ busy ? '正在删除…' : '确认删除' }}</button>
          <button class="button outline" :disabled="busy" @click="removing = null">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>
