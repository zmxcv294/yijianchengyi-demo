<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import AppIcon from '../components/AppIcon.vue'
import { api, ensureSession, errorText } from '../api/client'
import { projects } from '../api/projects'
import type { ProjectListItem, Scenario } from '../api/projects'
import { phaseLabels } from '../api/types'
import type { Task } from '../api/types'
const router = useRouter()
const items = ref<Task[]>([])
const projectItems = ref<ProjectListItem[]>([])
const loading = ref(true)
const error = ref('')
const pending = ref('')
const filter = ref<'all' | 'ready' | 'draft'>('all')
const search = ref('')
const editing = ref<Task | null>(null)
const removing = ref<Task | null>(null)
const newName = ref('')
const creating = ref(false)
const creatingBusy = ref(false)
const projectTitle = ref('')
const projectScenario = ref<Scenario>('poster')
const filtered = computed(() => items.value.filter(item =>
  (filter.value === 'all' || (filter.value === 'ready' ? item.phase === 'preview_ready' : item.phase !== 'preview_ready')) &&
  (item.displayName + ' ' + item.fields.title + ' ' + item.rawText).includes(search.value.trim())))
async function load() {
  loading.value = true; error.value = ''
  try {
    await ensureSession()
    items.value = (await api.tasks()).items
  } catch (cause) { error.value = errorText(cause) }
  finally { loading.value = false }
  // 项目列表失败不应拖垮整个页面（例如后端尚未部署多页接口）。
  try { projectItems.value = (await projects.list()).items }
  catch { projectItems.value = [] }
}
function projectMeta(item: ProjectListItem) {
  const kind = item.scenario === 'article_image' ? '公众号配图' : '竖版海报'
  const when = new Date(item.updatedAt).toLocaleString('zh-CN', { month: 'short', day: 'numeric' })
  return `${item.pageCount} 页 · ${kind} · ${when}`
}
async function rename() {
  if (!editing.value || !newName.value.trim()) return
  pending.value = editing.value.id; error.value = ''
  try {
    const updated = await api.rename(editing.value, newName.value.trim())
    items.value = items.value.map(item => item.id === updated.id ? updated : item)
    editing.value = null
  } catch (cause) { error.value = errorText(cause) }
  finally { pending.value = '' }
}
async function copy(item: Task) {
  pending.value = item.id; error.value = ''
  try { const created = await api.copy(item); await router.push('/create/' + created.id) }
  catch (cause) { error.value = errorText(cause) }
  finally { pending.value = '' }
}
async function remove() {
  if (!removing.value) return
  const item = removing.value
  pending.value = item.id; error.value = ''
  try { await api.remove(item); items.value = items.value.filter(value => value.id !== item.id); removing.value = null }
  catch (cause) { error.value = errorText(cause) }
  finally { pending.value = '' }
}
async function createProject() {
  if (creatingBusy.value || !projectTitle.value.trim()) return
  creatingBusy.value = true; error.value = ''
  try {
    const created = await projects.create(projectScenario.value, projectTitle.value.trim())
    creating.value = false; projectTitle.value = ''
    await router.push('/projects/' + created.id)
  } catch (cause) { error.value = errorText(cause) }
  finally { creatingBusy.value = false }
}
onMounted(load)
</script>
<template>
  <div class="page history-page"><div class="section-heading history-heading"><div><span class="eyebrow">每一份灵感，都有迹可循</span><h1>我的作品<span class="count">{{ items.length }}</span></h1><p class="muted">继续打磨草稿，或再次下载喜欢的作品。</p></div><div class="inline-actions"><button class="button outline" @click="creating = true; error = ''"><AppIcon name="plus" />新建多页项目</button><RouterLink class="button primary" to="/create"><AppIcon name="plus" />新建创作</RouterLink></div></div>
    <div class="history-toolbar"><div class="tabs" aria-label="筛选作品"><button :class="{ selected: filter === 'all' }" @click="filter = 'all'">全部作品</button><button :class="{ selected: filter === 'ready' }" @click="filter = 'ready'">已生成</button><button :class="{ selected: filter === 'draft' }" @click="filter = 'draft'">草稿与待处理</button></div><input v-model="search" class="search-input" placeholder="搜索作品名称或内容" aria-label="搜索作品" /></div>
    <div v-if="error" class="error-card" role="alert"><p>{{ error }}</p><button class="button outline" :disabled="!!pending" @click="load">重新加载</button></div>
    <section v-if="projectItems.length" class="history-projects" aria-label="多页项目">
      <div class="label-row"><h2>多页项目<span class="count">{{ projectItems.length }}</span></h2><span class="field-help">一个项目最多 10 页，逐页生成后导出多页 PDF</span></div>
      <div class="project-cards">
        <RouterLink v-for="item in projectItems" :key="item.id" :to="'/projects/' + item.id" class="project-card">
          <span class="project-card-cover">
            <img v-if="item.coverAssetId" :src="'/api/assets/' + item.coverAssetId + '/file'" :alt="item.title + '封面'" loading="lazy" />
            <AppIcon v-else name="grid" :size="26" />
          </span>
          <span class="project-card-copy"><strong>{{ item.title }}</strong><small>{{ projectMeta(item) }}</small></span>
        </RouterLink>
      </div>
    </section>
    <div v-if="loading" class="empty-state"><span class="spinner" /><h3>正在读取作品…</h3></div>
    <div v-else-if="!filtered.length && !error" class="empty-state"><span class="empty-icon"><AppIcon name="image" :size="32" /></span><h3>{{ search || filter !== 'all' ? '暂时没有符合条件的作品' : '把第一份灵感，留在这里。' }}</h3><p>创作页实时暂存文字；点击保存草稿后，会显示在作品列表中。</p><RouterLink class="button primary" to="/create">开始创作<AppIcon name="arrow" :size="16" /></RouterLink></div>
    <div v-else-if="!loading" class="work-grid history-grid"><article v-for="item in filtered" :key="item.id" class="work-card"><RouterLink :to="'/create/' + item.id" class="work-card-link"><div class="work-thumb"><img v-if="item.previewAssetId && item.previewRevision === item.revision" :src="'/api/assets/' + item.previewAssetId + '/file'" :alt="item.displayName || item.fields.title || '作品预览'" /><div v-else class="draft-art"><AppIcon name="file" :size="36" /><span>{{ item.fields.title || '等待灵感成形' }}</span></div><span class="pill">{{ phaseLabels[item.phase] }}</span></div><div class="work-card-copy"><h3>{{ item.displayName || item.fields.title || '未命名创作' }}</h3><p>{{ new Date(item.updatedAt).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }}<span>继续编辑 →</span></p></div></RouterLink><div class="work-management"><button class="text-button" :disabled="!!pending" @click="editing = item; newName = item.displayName || item.fields.title">重命名</button><button class="text-button" :disabled="!!pending || !!item.activeJob" @click="copy(item)">复制草稿</button><button class="text-button danger-text" :disabled="!!pending || !!item.activeJob" @click="removing = item">删除</button></div></article></div>
    <p class="session-note"><AppIcon name="clock" :size="16" />显示当前身份最近 50 份作品。匿名作品依赖当前会话 Cookie；退出或清除 Cookie 后不会自动混入其他身份。</p>
    <div v-if="editing" class="management-modal" role="dialog" aria-modal="true" aria-label="重命名作品"><form class="management-dialog" @submit.prevent="rename"><h2>重命名作品</h2><p class="field-help">只更改列表名称，保留正文、预览和事实确认。</p><label class="field-label" for="work-name">作品名称</label><input id="work-name" v-model="newName" maxlength="100" autofocus /><div class="inline-actions"><button class="button primary" :disabled="!!pending || !newName.trim()">保存名称</button><button type="button" class="button outline" :disabled="!!pending" @click="editing = null">取消</button></div><p v-if="error" role="alert">{{ error }}</p></form></div>
    <div v-if="removing" class="management-modal" role="dialog" aria-modal="true" aria-label="确认删除作品"><div class="management-dialog"><h2>删除这份作品？</h2><p>将删除「{{ removing.displayName || removing.fields.title || '未命名创作' }}」的草稿和生成记录，此操作无法撤销。</p><div class="inline-actions"><button class="button primary danger-button" :disabled="!!pending" @click="remove">确认删除</button><button class="button outline" :disabled="!!pending" @click="removing = null">取消</button></div><p v-if="error" role="alert">{{ error }}</p></div></div>
    <div v-if="creating" class="management-modal" role="dialog" aria-modal="true" aria-label="新建多页项目"><form class="management-dialog" @submit.prevent="createProject"><h2>新建多页项目</h2><p class="field-help">一个项目最多 10 页，每页独立选模板、确认和生成。</p><label class="field-label" for="project-title">项目名称</label><input id="project-title" v-model="projectTitle" maxlength="80" placeholder="例如：社团招新海报组" autofocus /><label class="field-label">作品类型</label><div class="scenario-options"><button type="button" :class="{ selected: projectScenario === 'poster' }" @click="projectScenario = 'poster'">竖版海报<small>1080 × 1440</small></button><button type="button" :class="{ selected: projectScenario === 'article_image' }" @click="projectScenario = 'article_image'">公众号配图<small>1200 × 675</small></button></div><div class="inline-actions"><button class="button primary" :disabled="creatingBusy || !projectTitle.trim()">{{ creatingBusy ? '正在创建…' : '创建项目' }}</button><button type="button" class="button outline" :disabled="creatingBusy" @click="creating = false">取消</button></div><p v-if="error" role="alert">{{ error }}</p></form></div>
  </div>
</template>
