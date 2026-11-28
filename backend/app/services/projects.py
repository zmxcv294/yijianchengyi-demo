"""Multi-page projects: an ordered list of ordinary tasks, one task per page.

Each page keeps the whole single-page pipeline (analysis, fact confirmation,
layout edits, revision-bound render jobs, preview assets). This module only adds
page structure, batch rendering and a multi-page raster PDF export. It never
changes the frozen v1 ``tasks`` table.

Two revisions are kept apart on purpose:
- ``projects.revision`` guards page structure (rename, add, delete, reorder);
- each page's ``tasks.revision`` guards its content, exactly as for single works.
"""

import io
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image

from app.errors import ApiError
from app.repositories.db import owned_row, save_task, task_payload
from app.services import management
from app.services.studio import now, require
from app.services.templates import FIELD_KEYS

MAX_PAGES = 10


def initialize(database):
    with database.transaction() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            owner_session_id TEXT NOT NULL REFERENCES sessions(id),
            scenario TEXT NOT NULL CHECK(scenario IN ('poster', 'article_image')),
            title TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS projects_owner_updated ON projects(owner_session_id, updated_at)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS project_pages (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK(position >= 0),
            PRIMARY KEY(project_id, task_id)
            )"""
        )


# ---------------------------------------------------------------- helpers


def _project_row(connection, identifier, owner):
    row = connection.execute(
        "SELECT * FROM projects WHERE id=? AND owner_session_id=?", (identifier, owner)
    ).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "未找到当前会话的多页项目")
    return row


def _project_revision_matches(row, expected):
    require(
        row["revision"] == expected,
        409,
        "PROJECT_REVISION_CONFLICT",
        "项目页面结构已被修改，请重新载入后重试",
        {"currentRevision": row["revision"]},
    )


def _page_ids(connection, project_id):
    return [
        row["task_id"]
        for row in connection.execute(
            "SELECT task_id FROM project_pages WHERE project_id=? ORDER BY position, task_id",
            (project_id,),
        ).fetchall()
    ]


def _write_order(connection, project_id, task_ids):
    for position, task_id in enumerate(task_ids):
        connection.execute(
            "UPDATE project_pages SET position=? WHERE project_id=? AND task_id=?",
            (position, project_id, task_id),
        )


def _bump(connection, project_id):
    connection.execute(
        "UPDATE projects SET revision=revision+1, updated_at=? WHERE id=?", (now(), project_id)
    )


def renumber_and_bump(connection, project_id):
    """Close position gaps after a page disappears; also called by management.delete_in."""
    _write_order(connection, project_id, _page_ids(connection, project_id))
    _bump(connection, project_id)


def _page_view(connection, task):
    full = management.view(connection, task)
    full["previewFresh"] = bool(
        task["previewAssetId"] and task["previewRevision"] == task["revision"]
    )
    return full


def _summary(pages):
    return {
        "total": len(pages),
        "fresh": sum(page["previewFresh"] for page in pages),
        "rendering": sum(page["activeJob"] is not None for page in pages),
        "failed": sum(page["phase"] == "failed" and page["activeJob"] is None for page in pages),
        "needsConfirmation": sum(
            page["confirmedRevision"] != page["revision"] and page["activeJob"] is None
            for page in pages
        ),
        "exportable": bool(pages) and all(page["previewFresh"] for page in pages),
    }


def _project_view(connection, row):
    pages = []
    for position, task_id in enumerate(_page_ids(connection, row["id"])):
        task = task_payload(owned_row(connection, "tasks", task_id, row["owner_session_id"]))
        pages.append({"position": position, **_page_view(connection, task)})
    return {
        "id": row["id"],
        "scenario": row["scenario"],
        "title": row["title"],
        "revision": row["revision"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "pages": pages,
        "summary": _summary(pages),
    }


def _new_page_task(scenario, style, raw_text):
    """Same initial record as Studio.create_task; kept here to share one transaction."""
    stamp = now()
    return {
        "id": str(uuid4()),
        "revision": 1,
        "scenario": scenario,
        "style": style,
        "rawText": raw_text,
        "fields": dict.fromkeys(FIELD_KEYS, ""),
        "userEditedFields": [],
        "assetIds": [],
        "templateId": None,
        "templateVersion": None,
        "analysis": None,
        "analysisMode": None,
        "confirmedRevision": None,
        "previewAssetId": None,
        "previewRevision": None,
        "phase": "draft",
        "lastError": None,
        "createdAt": stamp,
        "updatedAt": stamp,
    }


# ---------------------------------------------------------------- structure


def create_project(service, owner, payload):
    title = payload.title.strip()
    require(bool(title), 422, "VALIDATION_ERROR", "项目名称不能为空")
    stamp = now()
    identifier = str(uuid4())
    with service.db.transaction() as connection:
        connection.execute(
            "INSERT INTO projects VALUES (?,?,?,?,1,?,?)",
            (identifier, owner, payload.scenario, title, stamp, stamp),
        )
        return _project_view(connection, _project_row(connection, identifier, owner))


def list_projects(service, owner):
    with service.db.transaction() as connection:
        rows = connection.execute(
            "SELECT p.*, (SELECT count(*) FROM project_pages WHERE project_id=p.id) AS page_count "
            "FROM projects p WHERE owner_session_id=? ORDER BY updated_at DESC LIMIT 50",
            (owner,),
        ).fetchall()
        items = []
        for row in rows:
            first = connection.execute(
                "SELECT t.preview_asset_id FROM project_pages pp JOIN tasks t ON t.id=pp.task_id "
                "WHERE pp.project_id=? ORDER BY pp.position LIMIT 1",
                (row["id"],),
            ).fetchone()
            items.append({
                "id": row["id"],
                "scenario": row["scenario"],
                "title": row["title"],
                "revision": row["revision"],
                "pageCount": row["page_count"],
                "coverAssetId": first["preview_asset_id"] if first else None,
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            })
        return {"items": items}


def get_project(service, owner, identifier):
    with service.db.transaction() as connection:
        return _project_view(connection, _project_row(connection, identifier, owner))


def rename_project(service, owner, identifier, payload):
    title = payload.title.strip()
    require(bool(title), 422, "VALIDATION_ERROR", "项目名称不能为空")
    with service.db.transaction() as connection:
        row = _project_row(connection, identifier, owner)
        _project_revision_matches(row, payload.expected_revision)
        connection.execute("UPDATE projects SET title=? WHERE id=?", (title, identifier))
        _bump(connection, identifier)
        return _project_view(connection, _project_row(connection, identifier, owner))


def delete_project(service, owner, identifier, expected_revision):
    """Delete the project and every page task; refuses while any page is generating."""
    with service.db.transaction() as connection:
        row = _project_row(connection, identifier, owner)
        _project_revision_matches(row, expected_revision)
        task_ids = _page_ids(connection, identifier)
        busy = [
            task_id for task_id in task_ids
            if connection.execute(
                "SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running')", (task_id,)
            ).fetchone()
        ]
        require(not busy, 409, "TASK_BUSY", "有页面正在生成，请等待结束后删除项目", {"taskIds": busy})
        for task_id in task_ids:
            connection.execute("DELETE FROM jobs WHERE task_id=? AND owner_session_id=?", (task_id, owner))
            connection.execute("DELETE FROM tasks WHERE id=? AND owner_session_id=?", (task_id, owner))
        connection.execute("DELETE FROM projects WHERE id=?", (identifier,))


def add_page(service, owner, identifier, payload):
    """Append a blank page, or duplicate ``sourceTaskId`` right after the source page."""
    with service.db.transaction() as connection:
        row = _project_row(connection, identifier, owner)
        _project_revision_matches(row, payload.expected_revision)
        order = _page_ids(connection, identifier)
        require(
            len(order) < MAX_PAGES, 409, "PAGE_LIMIT", f"每个项目最多 {MAX_PAGES} 页",
            {"maxPages": MAX_PAGES},
        )
        if payload.source_task_id is not None:
            source = str(payload.source_task_id)
            require(source in order, 404, "NOT_FOUND", "要复制的页面不在这个项目里")
            require(payload.source_revision is not None, 422, "VALIDATION_ERROR",
                    "复制页面需要提供 sourceRevision")
            task = management.copy_in(connection, owner, source, payload.source_revision)
            order.insert(order.index(source) + 1, task["id"])
        else:
            raw_text = (payload.raw_text or "").strip()
            require(bool(raw_text), 422, "VALIDATION_ERROR", "请输入这一页的创作内容")
            task = _new_page_task(row["scenario"], payload.style, raw_text)
            save_task(connection, task, owner, insert=True)
            order.append(task["id"])
        connection.execute(
            "INSERT INTO project_pages VALUES (?,?,?)", (identifier, task["id"], len(order) - 1)
        )
        _write_order(connection, identifier, order)
        _bump(connection, identifier)
        return _project_view(connection, _project_row(connection, identifier, owner))


def delete_page(service, owner, identifier, task_id, expected_revision, page_revision):
    with service.db.transaction() as connection:
        row = _project_row(connection, identifier, owner)
        _project_revision_matches(row, expected_revision)
        require(task_id in _page_ids(connection, identifier), 404, "NOT_FOUND", "该页面不在这个项目里")
        # delete_in also renumbers positions and bumps the project revision.
        management.delete_in(connection, owner, task_id, page_revision)
        return _project_view(connection, _project_row(connection, identifier, owner))


def reorder(service, owner, identifier, payload):
    with service.db.transaction() as connection:
        row = _project_row(connection, identifier, owner)
        _project_revision_matches(row, payload.expected_revision)
        current = _page_ids(connection, identifier)
        wanted = [str(task_id) for task_id in payload.task_ids]
        require(
            len(wanted) == len(set(wanted)) and set(wanted) == set(current),
            422,
            "VALIDATION_ERROR",
            "排序必须恰好包含项目当前的全部页面",
            {"currentTaskIds": current},
        )
        if wanted != current:
            _write_order(connection, identifier, wanted)
            _bump(connection, identifier)
        return _project_view(connection, _project_row(connection, identifier, owner))


# ---------------------------------------------------------------- generation


def render_pages(service, owner, identifier, key: UUID):
    """Queue a render job for every confirmed page whose preview is stale.

    Each page is an independent job, so one failed page never blocks the others.
    Per-page idempotency keys are derived from the request key, so repeating the
    same request returns the same jobs. A retry after failure needs a new key.
    """
    with service.db.transaction() as connection:
        row = _project_row(connection, identifier, owner)
        pages = [
            task_payload(owned_row(connection, "tasks", task_id, owner))
            for task_id in _page_ids(connection, identifier)
        ]
    require(bool(pages), 409, "EMPTY_PROJECT", "项目还没有页面")
    queued, skipped, failed = [], [], []
    for task in pages:
        if task["previewAssetId"] and task["previewRevision"] == task["revision"]:
            skipped.append({"taskId": task["id"], "reason": "PREVIEW_FRESH"})
            continue
        if task["confirmedRevision"] != task["revision"]:
            skipped.append({"taskId": task["id"], "reason": "CONFIRMATION_REQUIRED"})
            continue
        try:
            job = service.enqueue(owner, task["id"], "render", task["revision"], f"{key}:{task['id']}")
            queued.append({"taskId": task["id"], "job": job})
        except ApiError as error:
            failed.append({"taskId": task["id"], "code": error.code, "message": error.message})
    return {"projectId": row["id"], "queued": queued, "skipped": skipped, "failed": failed}


def status(service, owner, identifier):
    """Lightweight polling payload for the progress bar; no fields or analysis."""
    keep = ("id", "position", "revision", "phase", "confirmedRevision", "previewAssetId",
            "previewRevision", "previewFresh", "activeJob", "lastError")
    with service.db.transaction() as connection:
        project = _project_view(connection, _project_row(connection, identifier, owner))
    return {
        "id": project["id"],
        "revision": project["revision"],
        "pages": [{name: page[name] for name in keep} for page in project["pages"]],
        "summary": project["summary"],
    }


# ---------------------------------------------------------------- export


@dataclass(frozen=True)
class ExportedProject:
    content: bytes
    pages: int


def build_pdf(pages: list[tuple[Path, str]]) -> ExportedProject:
    """Combine server-rendered page PNGs into one raster PDF, one page per image.

    At 72 dpi each page's size in PDF points equals its pixel size, as in the
    single-page export. Text is not selectable or editable.
    """
    images = []
    try:
        for path, background in pages:
            with Image.open(path) as source:
                source.load()
                if source.format != "PNG":
                    raise OSError("Preview must be a server-rendered PNG")
                rgba = source.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, background)
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            images.append(flattened)
        output = io.BytesIO()
        first, *rest = images
        first.save(output, "PDF", resolution=72.0, quality=95, save_all=True, append_images=rest)
        return ExportedProject(output.getvalue(), len(images))
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ApiError(
            503, "RENDER_FAILED", "多页导出暂时失败，请重试；各页预览和草稿不受影响"
        ) from None
    finally:
        for image in images:
            image.close()


def export_sources(service, owner, identifier, expected_revision):
    """Validate that every page has a current preview, then return (row, [(path, bg)])."""
    with service.db.transaction() as connection:
        row = _project_row(connection, identifier, owner)
        _project_revision_matches(row, expected_revision)
        pages = [
            task_payload(owned_row(connection, "tasks", task_id, owner))
            for task_id in _page_ids(connection, identifier)
        ]
        require(bool(pages), 409, "EMPTY_PROJECT", "项目还没有页面，无法导出")
        stale = [
            task["id"] for task in pages
            if not (task["previewAssetId"] and task["previewRevision"] == task["revision"])
        ]
        require(
            not stale, 409, "PREVIEW_STALE",
            "有页面尚无当前版本的预览，请先确认并生成这些页面",
            {"taskIds": stale},
        )
        templates = {}
        for task in pages:
            if task["templateId"] not in templates:
                templates[task["templateId"]] = service.template(task["templateId"])
    sources = []
    for task in pages:
        _, path = service.get_asset(owner, task["previewAssetId"])
        sources.append((path, templates[task["templateId"]]["canvas"]["background"]))
    return dict(row), sources

