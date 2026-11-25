"""Multi-page project endpoints (local v2 extension; the v1 contract is unchanged)."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from fastapi import Path as PathParam
from pydantic import Field, StrictInt, field_validator

from app.api.studio import IdempotencyKey, Owner, Service, TaskId
from app.models._base import ContractModel, ensure_unique
from app.models.templates import Scenario, Style
from app.services import projects

router = APIRouter(prefix="/api/v2/projects", tags=["多页项目"])
ProjectId = Annotated[UUID, PathParam(alias="projectId")]
Revision = Annotated[StrictInt, Field(ge=1)]


class ProjectCreate(ContractModel):
    scenario: Scenario
    title: Annotated[str, Field(min_length=1, max_length=80)]


class ProjectRename(ContractModel):
    expected_revision: Revision
    title: Annotated[str, Field(min_length=1, max_length=80)]


class PageCreate(ContractModel):
    """Blank page: give rawText (and optional style). Duplicate: give sourceTaskId + sourceRevision."""

    expected_revision: Revision
    raw_text: Annotated[str, Field(min_length=1, max_length=10000)] | None = None
    style: Style | None = None
    source_task_id: UUID | None = None
    source_revision: Revision | None = None


class PageOrder(ContractModel):
    expected_revision: Revision
    task_ids: Annotated[list[UUID], Field(min_length=1, max_length=projects.MAX_PAGES)]

    @field_validator("task_ids")
    @classmethod
    def _unique(cls, value):
        return ensure_unique(value)


@router.post("", status_code=201)
async def create_project(payload: ProjectCreate, service: Service, account: Owner):
    return projects.create_project(service, account, payload)


@router.get("")
async def list_projects(service: Service, account: Owner):
    return projects.list_projects(service, account)


@router.get("/{projectId}")
async def get_project(project_id: ProjectId, service: Service, account: Owner):
    return projects.get_project(service, account, str(project_id))


@router.patch("/{projectId}")
async def rename_project(project_id: ProjectId, payload: ProjectRename, service: Service, account: Owner):
    return projects.rename_project(service, account, str(project_id), payload)


@router.delete("/{projectId}", status_code=204)
async def delete_project(
    project_id: ProjectId, service: Service, account: Owner,
    expected_revision: Annotated[int, Query(alias="expectedRevision", ge=1)],
):
    projects.delete_project(service, account, str(project_id), expected_revision)
    return Response(status_code=204)


@router.post("/{projectId}/pages", status_code=201)
async def add_page(project_id: ProjectId, payload: PageCreate, service: Service, account: Owner):
    return projects.add_page(service, account, str(project_id), payload)


@router.delete("/{projectId}/pages/{taskId}")
async def delete_page(
    project_id: ProjectId, task_id: TaskId, service: Service, account: Owner,
    expected_revision: Annotated[int, Query(alias="expectedRevision", ge=1)],
    page_revision: Annotated[int, Query(alias="pageRevision", ge=1)],
):
    return projects.delete_page(
        service, account, str(project_id), str(task_id), expected_revision, page_revision
    )


@router.put("/{projectId}/order")
async def reorder(project_id: ProjectId, payload: PageOrder, service: Service, account: Owner):
    return projects.reorder(service, account, str(project_id), payload)


@router.post("/{projectId}/render", status_code=202)
async def render(project_id: ProjectId, key: IdempotencyKey, service: Service, account: Owner):
    return projects.render_pages(service, account, str(project_id), key)


@router.get("/{projectId}/status")
async def status(project_id: ProjectId, service: Service, account: Owner):
    return projects.status(service, account, str(project_id))


@router.get(
    "/{projectId}/export",
    response_class=Response,
    responses={
        200: {
            "description": "多页栅格 PDF，每页一张当前版本预览，文字不可编辑",
            "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
        },
    },
)
async def export(
    project_id: ProjectId, service: Service, account: Owner,
    expected_revision: Annotated[int, Query(alias="expectedRevision", ge=1)],
):
    row, sources = projects.export_sources(service, account, str(project_id), expected_revision)
    exported = await asyncio.to_thread(projects.build_pdf, sources)
    return Response(
        exported.content,
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="ycy-project-{row["id"]}.pdf"',
            "X-Export-Pages": str(exported.pages),
            "X-Export-Content": "raster-preview",
        },
    )
