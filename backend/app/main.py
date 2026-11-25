"""Application factory and startup configuration; business routers follow in later modules."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from app.config import PROJECT_ROOT, Settings, load_settings
from app.errors import error_response, install_error_handlers, unexpected_error
from app.services.studio import Studio

VERSION = "0.1.0"


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    mode: Literal["mock", "real"]
    version: str = Field(max_length=50)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = settings if settings is not None else load_settings()
        application.state.studio = Studio(application.state.settings)
        await application.state.studio.start()
        try:
            yield
        finally:
            await application.state.studio.stop()

    application = FastAPI(
        title="一键成艺 API",
        version=VERSION,
        lifespan=lifespan,
        debug=False,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    install_error_handlers(application)

    @application.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        if request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.url.path.startswith(
            "/api/"
        ):
            configuration = request.app.state.settings
            origin = request.headers.get("Origin")
            allowed = {item.strip() for item in configuration.app_allowed_origins.split(",")}
            if (origin is not None and origin not in allowed) or (
                origin is None and configuration.app_env == "production"
            ):
                return error_response(
                    request, 403, "ORIGIN_NOT_ALLOWED", "请求来源不受允许，请从应用页面重新操作"
                )
        try:
            response = await call_next(request)
        except Exception as exc:
            response = await unexpected_error(request, exc)
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @application.get("/api/health", response_model=Health, operation_id="getHealth")
    async def health(request: Request) -> Health:
        return Health(status="ok", mode=request.app.state.settings.llm_mode, version=VERSION)

    from app.api.accounts_v2 import router as accounts_router
    from app.api.exports_v2 import router as exports_router
    from app.api.layouts_v2 import router as layouts_router
    from app.api.management_v2 import router as management_router
    from app.api.materials_v2 import router as materials_router
    from app.api.notifications_v2 import router as notifications_router
    from app.api.operations_v2 import router as operations_router
    from app.api.projects_v2 import router as projects_router
    from app.api.studio import router as studio_router
    from app.api.template_workbench_v2 import router as workbench_router
    from app.api.templates import router as templates_router

    application.include_router(templates_router)
    application.include_router(studio_router)
    application.include_router(management_router)
    application.include_router(accounts_router)
    application.include_router(exports_router)
    application.include_router(materials_router)
    application.include_router(notifications_router)
    application.include_router(layouts_router)
    application.include_router(workbench_router)
    application.include_router(operations_router)
    application.include_router(projects_router)

    @application.get("/", include_in_schema=False)
    @application.get("/create", include_in_schema=False)
    @application.get("/history", include_in_schema=False)
    @application.get("/account", include_in_schema=False)
    @application.get("/materials", include_in_schema=False)
    @application.get("/templates-workbench", include_in_schema=False)
    @application.get("/create/{frontend_path}", include_in_schema=False)
    @application.get("/assets/{frontend_path:path}", include_in_schema=False)
    async def frontend(request: Request, frontend_path: str = ""):
        from app.errors import ApiError

        frontend_path = request.url.path.lstrip("/")
        directory = (PROJECT_ROOT / "frontend/dist").resolve()
        candidate = (directory / frontend_path).resolve()
        if not candidate.is_relative_to(directory):
            raise ApiError(404, "NOT_FOUND", "未找到请求的文件")
        if candidate.is_file():
            return FileResponse(candidate)
        index = directory / "index.html"
        if index.is_file() and "." not in frontend_path.rsplit("/", 1)[-1]:
            return FileResponse(index)
        raise ApiError(404, "NOT_FOUND", "前端尚未构建，请先运行 npm run build")

    return application


app = create_app()
