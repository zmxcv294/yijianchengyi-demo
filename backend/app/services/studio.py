"""Task lifecycle, session isolation and persistent revision-bound jobs."""

import asyncio
import hashlib
import io
import json
import logging
import secrets
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from app.errors import ApiError
from app.repositories.db import Database, encode, owned_row, save_task, task_payload
from app.services.templates import (
    FIELD_KEYS,
    TemplateCatalog,
    TemplateNotFoundError,
    field_capacity_errors,
    missing_required_fields,
)

logger = logging.getLogger(__name__)
MAX_UPLOAD = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000


def now():
    return datetime.now(UTC).isoformat()


def require(condition, status, code, message, details=None):
    if not condition:
        raise ApiError(status, code, message, details)


def revision_matches(task, revision):
    require(
        task["revision"] == revision,
        409,
        "REVISION_CONFLICT",
        "草稿已更新，请载入最新版本；当前文字仍可复制保留",
        {"currentRevision": task["revision"]},
    )


def job_payload(row):
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "kind": row["kind"],
        "revision": row["revision"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
        "createdAt": row["created_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
    }


def asset_payload(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "mimeType": row["mime_type"],
        "bytes": row["bytes"],
        "width": row["width"],
        "height": row["height"],
        "fileUrl": f"/api/assets/{row['id']}/file",
        "createdAt": row["created_at"],
    }


class Studio:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.catalog = TemplateCatalog(thumbnail_dir=settings.data_dir / "template-previews")
        self.wake = asyncio.Event()
        self.workers = []

    async def start(self):
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db.initialize()
        from app.services.accounts import initialize as initialize_accounts
        from app.services.layouts import initialize as initialize_layouts
        from app.services.management import initialize as initialize_management
        from app.services.materials import initialize as initialize_materials
        from app.services.projects import initialize as initialize_projects
        from app.services.template_workbench import initialize as initialize_workbench

        initialize_accounts(self.db)
        initialize_management(self.db)
        initialize_projects(self.db)
        initialize_materials(self.db)
        initialize_layouts(self.db)
        initialize_workbench(self.db)
        with self.db.transaction() as connection:
            error = {
                "code": "WORKER_RESTARTED",
                "message": "服务重启中断了生成，草稿已保留，请手动重试",
                "details": {},
                "retryable": True,
            }
            for row in connection.execute("SELECT * FROM jobs WHERE status='running'").fetchall():
                connection.execute(
                    "UPDATE jobs SET status='failed',stage='failed',error_json=?,finished_at=? WHERE id=?",
                    (encode(error), now(), row["id"]),
                )
                task = task_payload(
                    owned_row(connection, "tasks", row["task_id"], row["owner_session_id"])
                )
                if task["revision"] == row["revision"]:
                    task.update(phase="failed", lastError=error, updatedAt=now())
                    save_task(connection, task, row["owner_session_id"])
        self.workers = [
            asyncio.create_task(self.worker(), name=f"studio-worker-{index}")
            for index in range(self.settings.job_concurrency)
        ]

    async def stop(self):
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    def session(self, token):
        if not token:
            raise ApiError(
                401, "SESSION_REQUIRED", "会话已过期，请重新建立会话；未保存文字可复制保留"
            )
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (digest, now())
            ).fetchone()
        require(row is not None, 401, "SESSION_REQUIRED", "请先建立或续期会话")
        from app.services.accounts import workspace

        with self.db.transaction() as connection:
            return workspace(connection, row["id"])

    def create_session(self, token):
        if token:
            from app.services.accounts import recover_anonymous_after_revocation

            recovered = recover_anonymous_after_revocation(self, token)
            if recovered:
                token = recovered
        expires = (datetime.now(UTC) + timedelta(days=self.settings.session_ttl_days)).isoformat()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?",
                (hashlib.sha256((token or "").encode()).hexdigest(), now()),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE sessions SET expires_at=? WHERE id=?", (expires, row["id"])
                )
            else:
                token = secrets.token_urlsafe(32)
                connection.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?)",
                    (str(uuid4()), hashlib.sha256(token.encode()).hexdigest(), now(), expires),
                )
        return token, expires

    def template(self, identifier):
        if identifier.startswith("draft-"):
            from app.services.template_workbench import get_published

            return get_published(self.db, identifier)
        try:
            return self.catalog.get_template(identifier)
        except TemplateNotFoundError:
            raise ApiError(404, "NOT_FOUND", "未找到所选模板") from None

    def validate_assets(self, connection, identifiers, owner):
        for identifier in identifiers:
            row = owned_row(connection, "assets", identifier, owner)
            require(row["kind"] == "upload", 422, "VALIDATION_ERROR", "只能使用本会话上传的原图")

    def task_template(self, connection, task):
        if not task["templateId"]:
            return None
        from app.services.layouts import effective

        return effective(connection, task, self.template(task["templateId"]))

    def create_task(self, owner, payload):
        values = payload.model_dump(mode="json", by_alias=True)
        require(bool(values["rawText"].strip()), 422, "VALIDATION_ERROR", "请输入创作内容")
        stamp = now()
        task = {
            "id": str(uuid4()),
            "revision": 1,
            **values,
            "fields": dict.fromkeys(FIELD_KEYS, ""),
            "userEditedFields": [],
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
        with self.db.transaction() as connection:
            self.validate_assets(connection, task["assetIds"], owner)
            save_task(connection, task, owner, insert=True)
        return task

    def get_task(self, owner, identifier):
        with self.db.transaction() as connection:
            return task_payload(owned_row(connection, "tasks", identifier, owner))

    def list_tasks(self, owner):
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE owner_session_id=? ORDER BY updated_at DESC LIMIT 50",
                (owner,),
            ).fetchall()
        return {"items": [task_payload(row) for row in rows]}

    def patch_task(self, owner, identifier, payload):
        changes = payload.model_dump(mode="json", by_alias=True, exclude_unset=True)
        revision = changes.pop("expectedRevision")
        for key in ("rawText", "scenario", "fields", "assetIds"):
            require(
                key not in changes or changes[key] is not None,
                422,
                "VALIDATION_ERROR",
                "此字段不能设为空值",
            )
        with self.db.transaction() as connection:
            task = task_payload(owned_row(connection, "tasks", identifier, owner))
            revision_matches(task, revision)
            changes = {key: value for key, value in changes.items() if task[key] != value}
            if not changes:
                return task
            if "rawText" in changes:
                require(bool(changes["rawText"].strip()), 422, "VALIDATION_ERROR", "请输入创作内容")
            if "assetIds" in changes:
                self.validate_assets(connection, changes["assetIds"], owner)
            if "fields" in changes:
                edited = set(task["userEditedFields"])
                edited.update(
                    key for key in FIELD_KEYS if changes["fields"][key] != task["fields"][key]
                )
                task["userEditedFields"] = [key for key in FIELD_KEYS if key in edited]
            task.update(changes)
            if task["templateId"]:
                template = self.template(task["templateId"])
                if template["scenario"] != task["scenario"] or (
                    task["style"] and template["style"] != task["style"]
                ):
                    require(
                        "templateId" not in changes,
                        422,
                        "VALIDATION_ERROR",
                        "所选模板与作品类型或风格不匹配",
                    )
                    task.update(templateId=None, templateVersion=None)
                else:
                    task["templateVersion"] = template["version"]
            else:
                task["templateVersion"] = None
            task.update(
                revision=task["revision"] + 1,
                confirmedRevision=None,
                previewAssetId=None,
                previewRevision=None,
                lastError=None,
                updatedAt=now(),
            )
            if set(changes) <= {"assetIds"} and task["analysis"]:
                task["phase"] = (
                    "needs_input"
                    if not task["templateId"] or task["analysis"]["missingFields"]
                    else "needs_confirmation"
                )
            else:
                task.update(analysis=None, analysisMode=None, phase="draft")
            save_task(connection, task, owner)
        return task

    def validate_content(self, task, template):
        analysis = task["analysis"]
        require(analysis is not None, 409, "CONFIRMATION_REQUIRED", "请先分析当前版本的内容")
        missing = missing_required_fields(template, analysis["intentType"], task["fields"])
        require(
            not missing, 422, "MISSING_FIELDS", "请补齐必填信息后再生成", {"fields": list(missing)}
        )
        errors = field_capacity_errors(template, task["fields"])
        require(
            not errors,
            422,
            "TEXT_OVERFLOW",
            "文字超过模板容量，请精简内容或更换模板",
            {"fields": list(errors)},
        )

    def confirm(self, owner, identifier, payload):
        with self.db.transaction() as connection:
            task = task_payload(owned_row(connection, "tasks", identifier, owner))
            revision_matches(task, payload.expected_revision)
            require(
                bool(task["templateId"]), 409, "CONFIRMATION_REQUIRED", "请先选择模板并分析内容"
            )
            self.validate_content(task, self.task_template(connection, task))
            required = {fact["id"] for fact in task["analysis"]["facts"]}
            require(
                set(payload.confirmed_fact_ids) == required,
                409,
                "CONFIRMATION_REQUIRED",
                "请逐项确认当前版本的全部关键事实",
            )
            require(
                not connection.execute(
                    "SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running')",
                    (identifier,),
                ).fetchone(),
                409,
                "TASK_BUSY",
                "当前任务正在处理，请稍候",
            )
            task.update(
                confirmedRevision=task["revision"],
                phase="ready_to_render",
                lastError=None,
                updatedAt=now(),
            )
            save_task(connection, task, owner)
        return task

    def enqueue(self, owner, identifier, kind, revision, key):
        require(
            bool(key) and len(key) <= 200,
            422,
            "VALIDATION_ERROR",
            "生成请求必须提供不超过200字符的幂等键",
        )
        digest = hashlib.sha256(
            encode(
                ["POST", f"/api/tasks/{identifier}/{kind}", {"expectedRevision": revision}]
            ).encode()
        ).hexdigest()
        with self.db.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM jobs WHERE owner_session_id=? AND idempotency_key=?", (owner, key)
            ).fetchone()
            if previous:
                require(
                    previous["request_hash"] == digest,
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "该幂等键已用于其他请求",
                )
                return job_payload(previous)
            task = task_payload(owned_row(connection, "tasks", identifier, owner))
            revision_matches(task, revision)
            busy = connection.execute(
                "SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running')",
                (identifier,),
            ).fetchone()
            require(not busy, 409, "TASK_BUSY", "当前任务正在处理，请稍候再试")
            count = connection.execute(
                "SELECT count(*) FROM jobs WHERE status='queued'"
            ).fetchone()[0]
            require(
                count < self.settings.job_queue_limit,
                429,
                "QUEUE_FULL",
                "生成队列已满，草稿已保留，请稍后重试",
            )
            template = self.task_template(connection, task)
            if kind == "render":
                require(
                    template is not None and task["confirmedRevision"] == task["revision"],
                    409,
                    "CONFIRMATION_REQUIRED",
                    "请确认当前版本的关键事实后再生成",
                )
                self.validate_content(task, template)
                self.validate_assets(connection, task["assetIds"], owner)
            job_id = str(uuid4())
            connection.execute(
                "INSERT INTO jobs (id,owner_session_id,task_id,kind,revision,snapshot_json,idempotency_key,request_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    owner,
                    identifier,
                    kind,
                    revision,
                    encode({"task": task, "template": template}),
                    key,
                    digest,
                    now(),
                ),
            )
            task.update(lastError=None, updatedAt=now())
            if kind == "render":
                task["phase"] = "rendering"
            save_task(connection, task, owner)
            job = job_payload(owned_row(connection, "jobs", job_id, owner))
        self.wake.set()
        return job

    def get_job(self, owner, identifier):
        with self.db.transaction() as connection:
            return job_payload(owned_row(connection, "jobs", identifier, owner))

    def get_asset(self, owner, identifier):
        with self.db.transaction() as connection:
            row = owned_row(connection, "assets", identifier, owner)
        path = (self.settings.data_dir / row["path"]).resolve()
        require(
            path.is_relative_to(self.settings.data_dir.resolve()) and path.is_file(),
            404,
            "NOT_FOUND",
            "图片文件暂不可用，请重新生成或上传",
        )
        return row, path

    def store_image(self, owner, content, kind, width, height, mime):
        identifier = str(uuid4())
        relative = f"assets/{identifier}.{'png' if mime == 'image/png' else 'jpg'}"
        path = self.settings.data_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
        asset = {
            "id": identifier,
            "owner": owner,
            "path": relative,
            "mime": mime,
            "bytes": len(content),
            "width": width,
            "height": height,
            "kind": kind,
        }
        return asset

    def insert_asset(self, connection, asset):
        connection.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
            (
                asset["id"],
                asset["owner"],
                asset["kind"],
                asset["path"],
                asset["mime"],
                asset["bytes"],
                asset["width"],
                asset["height"],
                now(),
            ),
        )

    def upload(self, owner, content, filename):
        require(len(content) <= MAX_UPLOAD, 413, "FILE_TOO_LARGE", "单张图片不能超过10 MiB")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    fmt, width, height = image.format, *image.size
                    require(
                        fmt in {"PNG", "JPEG"},
                        415,
                        "UNSUPPORTED_IMAGE",
                        "仅支持实际可解码的 JPG 和 PNG 图片",
                    )
                    require(
                        width * height <= MAX_PIXELS,
                        415,
                        "UNSUPPORTED_IMAGE",
                        "图片像素不能超过4000万",
                    )
                    image.verify()
                with Image.open(io.BytesIO(content)) as image:
                    image.load()
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise ApiError(
                415, "UNSUPPORTED_IMAGE", "图片无法解码，请上传有效的 JPG 或 PNG"
            ) from None
        suffix = Path(filename or "").suffix.lower()
        require(
            suffix in ({".png"} if fmt == "PNG" else {".jpg", ".jpeg"}),
            415,
            "UNSUPPORTED_IMAGE",
            "文件扩展名与实际图片格式不符",
        )
        asset = self.store_image(
            owner, content, "upload", width, height, "image/png" if fmt == "PNG" else "image/jpeg"
        )
        try:
            with self.db.transaction() as connection:
                self.insert_asset(connection, asset)
                row = owned_row(connection, "assets", asset["id"], owner)
        except BaseException:
            (self.settings.data_dir / asset["path"]).unlink(missing_ok=True)
            raise
        return asset_payload(row)

    async def worker(self):
        while True:
            with self.db.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1"
                ).fetchone()
                if row:
                    connection.execute(
                        "UPDATE jobs SET status='running',stage=?,progress=10,started_at=? WHERE id=?",
                        (
                            "classifying" if row["kind"] == "analyze" else "adapting_images",
                            now(),
                            row["id"],
                        ),
                    )
            if row:
                await self.execute(dict(row))
            else:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=0.4)
                except TimeoutError:
                    pass

    async def execute(self, job):
        snapshot = json.loads(job["snapshot_json"])
        owner, identifier = job["owner_session_id"], job["task_id"]
        asset = None
        try:
            if job["kind"] == "analyze":
                from app.services.analysis import analyze

                templates = [self.template(item.id) for item in self.catalog.list_templates().items]
                analysis = await analyze(
                    snapshot["task"], templates, snapshot["template"], self.settings
                )
            else:
                from app.rendering.renderer import render_template

                image_path = None
                if snapshot["task"]["assetIds"]:
                    _, image_path = self.get_asset(owner, snapshot["task"]["assetIds"][0])
                content = await render_template(
                    snapshot["template"], snapshot["task"]["fields"], image_path
                )
                canvas = snapshot["template"]["canvas"]
                asset = self.store_image(
                    owner, content, "preview", canvas["width"], canvas["height"], "image/png"
                )
            with self.db.transaction() as connection:
                task = task_payload(owned_row(connection, "tasks", identifier, owner))
                if task["revision"] != job["revision"]:
                    self.finish(connection, job["id"], "superseded")
                else:
                    if job["kind"] == "analyze":
                        task.update(
                            fields=analysis["fields"],
                            analysis=analysis,
                            analysisMode=self.settings.llm_mode,
                            revision=task["revision"] + 1,
                            confirmedRevision=None,
                            previewAssetId=None,
                            previewRevision=None,
                            phase="needs_confirmation"
                            if snapshot["template"] and not analysis["missingFields"]
                            else "needs_input",
                        )
                        result = {"appliedRevision": task["revision"]}
                    else:
                        self.insert_asset(connection, asset)
                        task.update(
                            previewAssetId=asset["id"],
                            previewRevision=task["revision"],
                            phase="preview_ready",
                        )
                        result = {
                            "appliedRevision": task["revision"],
                            "previewAssetId": asset["id"],
                        }
                    task.update(lastError=None, updatedAt=now())
                    save_task(connection, task, owner)
                    self.finish(connection, job["id"], "succeeded", result=result)
                    asset = None
        except ApiError as error:
            self.fail_job(job, error.code, error.message, error.details, error.status_code >= 500)
        except Exception as error:
            logger.error("生成失败 jobId=%s exceptionType=%s", job["id"], type(error).__name__)
            self.fail_job(
                job,
                "RENDER_FAILED" if job["kind"] == "render" else "MODEL_UNAVAILABLE",
                "本次处理失败，草稿已保留，请重试",
                {},
                True,
            )
        finally:
            if asset:
                (self.settings.data_dir / asset["path"]).unlink(missing_ok=True)

    def finish(self, connection, identifier, status, *, result=None, error=None):
        stage = "done" if status == "succeeded" else status
        connection.execute(
            "UPDATE jobs SET status=?,stage=?,progress=100,result_json=?,error_json=?,finished_at=? WHERE id=?",
            (
                status,
                stage,
                encode(result) if result else None,
                encode(error) if error else None,
                now(),
                identifier,
            ),
        )

    def fail_job(self, job, code, message, details, retryable):
        error = {"code": code, "message": message, "details": details, "retryable": retryable}
        with self.db.transaction() as connection:
            task = task_payload(
                owned_row(connection, "tasks", job["task_id"], job["owner_session_id"])
            )
            if task["revision"] == job["revision"]:
                task.update(phase="failed", lastError=error, updatedAt=now())
                save_task(connection, task, job["owner_session_id"])
                self.finish(connection, job["id"], "failed", error=error)
            else:
                self.finish(connection, job["id"], "superseded")
