# 多页项目接口与已知限制

本文件独立于 `docs/API.md`，以免触发契约快照哈希校验；契约 MR 获组内批准后可再并入。

## 接口（`/api/v2/projects`）

一个项目是**按顺序排列的一组普通作品（task），一页一个 task**。每页继续使用原有的分析、事实确认、排版编辑（`/api/v2/tasks/{taskId}/layout`）、生成和预览接口，本组接口只管页面结构、批量生成和多页导出。`tasks` 表结构没有改动。

两层版本号分开使用：

- 项目 `revision`：改名、加页、删页、排序时校验并 +1，冲突返回 409 `PROJECT_REVISION_CONFLICT`（`details.currentRevision`）。
- 每页 task 的 `revision`：内容修改时照旧校验，与项目版本号无关。

| 方法与路径 | 请求 | 说明 |
| --- | --- | --- |
| `POST /api/v2/projects` | `{scenario, title}` | 新建空项目；`scenario` 为 `poster` 或 `article_image`，新页面继承该值 |
| `GET /api/v2/projects` | — | 列表：`pageCount`、`coverAssetId`（第一页预览，可能为空） |
| `GET /api/v2/projects/{projectId}` | — | 详情：`pages[]` 为按顺序的完整 task（含 `position`、`previewFresh`、`activeJob`），附 `summary` |
| `PATCH /api/v2/projects/{projectId}` | `{expectedRevision, title}` | 改名 |
| `DELETE /api/v2/projects/{projectId}?expectedRevision=` | — | 删除项目及其全部页面；有页面在生成时 409 `TASK_BUSY` |
| `POST /api/v2/projects/{projectId}/pages` | 空白页：`{expectedRevision, rawText, style?}`；复制页：`{expectedRevision, sourceTaskId, sourceRevision}` | 空白页追加到末尾；复制页插在源页之后，连同排版改动一起复制，副本为草稿状态 |
| `DELETE /api/v2/projects/{projectId}/pages/{taskId}?expectedRevision=&pageRevision=` | — | 删除该页（即删除该 task），位置自动重排 |
| `PUT /api/v2/projects/{projectId}/order` | `{expectedRevision, taskIds}` | 传完整顺序；必须恰好是当前全部页面，否则 422 |
| `POST /api/v2/projects/{projectId}/render` | 头 `Idempotency-Key: <uuid>` | 202。为“已确认且预览过期”的每一页各排一个生成任务，返回 `queued[]`（含 job）、`skipped[]`（`PREVIEW_FRESH`/`CONFIRMATION_REQUIRED`）、`failed[]`（如 `QUEUE_FULL`、`TASK_BUSY`） |
| `GET /api/v2/projects/{projectId}/status` | — | 轮询用的精简进度：每页 `phase`/`previewFresh`/`activeJob`/`lastError`，`summary` 含 `total`/`fresh`/`rendering`/`failed`/`needsConfirmation`/`exportable` |
| `GET /api/v2/projects/{projectId}/export?expectedRevision=` | — | 多页 PDF，每页一张当前版本预览，响应头 `X-Export-Pages`；有页面预览过期返回 409 `PREVIEW_STALE`，`details.taskIds` 列出这些页；空项目 409 `EMPTY_PROJECT` |

使用顺序：建项目 → 加页 → 每页选模板、分析、确认（沿用单页接口）→ `render` → 轮询 `status` 或各 job → `summary.exportable` 为真后导出。缩略图直接用每页的 `previewAssetId` 调 `/api/assets/{assetId}/file`。`/api/v2/tasks` 列表中每个作品带 `projectId`，独立作品为 `null`，前端可据此在“我的作品”中隐藏或归组项目页面。

## 已知限制（本轮记录，暂不处理）

1. **生成队列上限是全局的**：`job_queue_limit` 默认且最大 20，所有用户共用。一个项目最多 10 页（`MAX_PAGES`），一次批量生成可能占掉一半队列；排不进去的页面出现在 `failed[]` 中，稍后换新的 `Idempotency-Key` 重试即可。
2. **重试必须换新的 `Idempotency-Key`**：同一个 key 重复请求返回原来的 job（包括已失败的 job）。重试只会重新排队失败或过期的页面，已完成的页面会被跳过。
3. **一页失败不影响其他页**：每页是独立 job；失败页的原因在 `status.pages[].lastError` 中，导出会因该页过期而返回 409。
4. **PDF 是栅格图**：文字不可选择、不可编辑；可编辑 PPTX 不在本轮范围。
5. **一个项目只放一种 scenario**：新页面继承项目的 scenario。单页接口仍允许把某页改成另一种 scenario，这时导出的 PDF 会出现横竖混排（每页保持自己的尺寸），后端目前不拦截。
6. **作品列表上限**：`/api/v2/tasks` 最多返回 50 条，项目页面也计入其中，页面多时独立作品可能被挤出列表；需要时再加分页或 `standalone` 过滤。
7. **暂不支持自动拆页**：目前只能手动一页一页添加。“一段原文自动拆成多页”需要和本地大模型联调，等模型侧完成后再评估。
8. **匿名作品导入账号时，项目会随页面一起迁移**（已处理）；导入前若有页面在生成会被现有检查拦下。
