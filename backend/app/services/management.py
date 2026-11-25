"""Additive local SRS task management without changing the frozen v1 contract."""

from uuid import uuid4

from app.repositories.db import owned_row, save_task, task_payload
from app.services.studio import job_payload, now, require, revision_matches


def initialize(database):
    with database.transaction() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS task_labels (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            management_revision INTEGER NOT NULL CHECK(management_revision >= 1)
            )"""
        )


def view(connection, task):
    label = connection.execute(
        "SELECT display_name,management_revision FROM task_labels WHERE task_id=?", (task["id"],)
    ).fetchone()
    active = connection.execute(
        "SELECT * FROM jobs WHERE task_id=? AND status IN ('queued','running')", (task["id"],)
    ).fetchone()
    page = connection.execute(
        "SELECT project_id FROM project_pages WHERE task_id=?", (task["id"],)
    ).fetchone()
    return {
        **task,
        "projectId": page["project_id"] if page else None,
        "displayName": label["display_name"] if label else None,
        "managementRevision": label["management_revision"] if label else 0,
        "activeJob": job_payload(active) if active else None,
    }


def get_task(service, owner, identifier):
    with service.db.transaction() as connection:
        return view(connection, task_payload(owned_row(connection, "tasks", identifier, owner)))


def list_tasks(service, owner):
    with service.db.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE owner_session_id=? ORDER BY updated_at DESC LIMIT 50",
            (owner,),
        ).fetchall()
        return {"items": [view(connection, task_payload(row)) for row in rows]}


def rename(service, owner, identifier, payload):
    name = payload.display_name.strip()
    require(bool(name), 422, "VALIDATION_ERROR", "作品名称不能为空")
    with service.db.transaction() as connection:
        task = task_payload(owned_row(connection, "tasks", identifier, owner))
        revision_matches(task, payload.expected_revision)
        current = view(connection, task)
        require(
            current["managementRevision"] == payload.expected_management_revision,
            409, "MANAGEMENT_CONFLICT", "作品名称已被修改，请重新载入后重试",
            {"currentManagementRevision": current["managementRevision"]},
        )
        connection.execute(
            """INSERT INTO task_labels VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET
            display_name=excluded.display_name,management_revision=excluded.management_revision""",
            (identifier, name, current["managementRevision"] + 1),
        )
        return view(connection, task)


def copy_in(connection, owner, identifier, revision):
    """Copy one task inside the caller's transaction (also used by project pages)."""
    source = task_payload(owned_row(connection, "tasks", identifier, owner))
    revision_matches(source, revision)
    label = view(connection, source)["displayName"] or source["fields"]["title"] or "未命名作品"
    task = dict(source)
    stamp = now()
    task.update(
        id=str(uuid4()), revision=1, analysis=None, analysisMode=None,
        confirmedRevision=None, previewAssetId=None, previewRevision=None,
        phase="draft", lastError=None, createdAt=stamp, updatedAt=stamp,
    )
    save_task(connection, task, owner, insert=True)
    layout = connection.execute("SELECT template_id,document_json FROM task_layouts WHERE task_id=?", (identifier,)).fetchone()
    if layout:
        connection.execute("INSERT INTO task_layouts VALUES (?,?,?)", (task["id"], layout["template_id"], layout["document_json"]))
    connection.execute("INSERT INTO task_labels VALUES (?,?,1)", (task["id"], f"{label[:74]}（副本）"))
    return task


def copy_task(service, owner, identifier, revision):
    with service.db.transaction() as connection:
        return view(connection, copy_in(connection, owner, identifier, revision))


def delete_in(connection, owner, identifier, revision):
    """Delete one task inside the caller's transaction (also used by project pages)."""
    task = task_payload(owned_row(connection, "tasks", identifier, owner))
    revision_matches(task, revision)
    require(
        view(connection, task)["activeJob"] is None, 409, "TASK_BUSY",
        "作品正在生成，请等待任务结束后删除",
    )
    page = connection.execute(
        "SELECT project_id FROM project_pages WHERE task_id=?", (identifier,)
    ).fetchone()
    connection.execute("DELETE FROM jobs WHERE task_id=? AND owner_session_id=?", (identifier, owner))
    # project_pages, task_layouts and task_labels rows cascade with the task.
    connection.execute("DELETE FROM tasks WHERE id=? AND owner_session_id=?", (identifier, owner))
    # Uploaded and preview assets remain private to their owner. Copies may reuse them.
    if page:
        from app.services.projects import renumber_and_bump

        renumber_and_bump(connection, page["project_id"])


def delete_task(service, owner, identifier, revision):
    with service.db.transaction() as connection:
        delete_in(connection, owner, identifier, revision)
