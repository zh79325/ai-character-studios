"""FastAPI 应用装配。

只监听 127.0.0.1，不做鉴权——它的唯一客户端是本机 Electron。CORS 放开 Vite 开发端口，
打包后前端走 file:// 协议（Origin 为 null），因此也一并允许。
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from atelier.api import characters, config, conversations, events, providers
from atelier.api import projects as projects_api
from atelier.api.deps import RuntimeDb
from atelier.api.portable import PortableError
from atelier.assets import projects
from atelier.assets.layout import LayoutError
from atelier.errors import Conflict, NotFound
from atelier.providers.base import NoCandidateError, ProviderError
from atelier.providers.period import PeriodExprError
from atelier.settings import get_settings

_log = structlog.get_logger(__name__)

DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Atelier 后端",
        version="0.1.0",
        summary="AI 素材与动画制作工具的本地后端",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_origin_regex=r"^file://.*$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(providers.router)
    app.include_router(config.router)
    app.include_router(events.router)
    app.include_router(projects_api.router)
    app.include_router(conversations.router)
    app.include_router(conversations.memory_router)
    app.include_router(characters.router)

    _install_error_handlers(app)

    @app.get("/api/health", tags=["health"])
    def health(session: RuntimeDb) -> dict[str, Any]:
        """Electron 起完后端后靠这个确认真的能服务了。项目库跟着打开的项目走，没打开则为 null。"""
        settings = get_settings()
        ref = projects.opened(session)
        return {
            "ok": True,
            "config_db": str(settings.config_db_path),
            "runtime_db": str(settings.runtime_db_path),
            "usage_server": settings.usage_server_url or None,
            "opened_project": ref.code if ref else None,
            "project_db": str(ref.db_path) if ref else None,
        }

    return app


def _install_error_handlers(app: FastAPI) -> None:
    """领域异常翻译成 HTTP 语义，路由函数里就不用到处 try 了。"""

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def _conflict(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PortableError)
    async def _portable(_: Request, exc: PortableError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": f"配置包不合法：{exc}"})

    @app.exception_handler(LayoutError)
    async def _layout(_: Request, exc: LayoutError) -> JSONResponse:
        # 目录名不合规或路径越出项目目录：是入参问题，不是服务器问题
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(PeriodExprError)
    async def _period(_: Request, exc: PeriodExprError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(NoCandidateError)
    async def _no_candidate(_: Request, exc: NoCandidateError) -> JSONResponse:
        # 不是服务器坏了，是配置或额度不允许——503 让前端提示去设置页
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ProviderError)
    async def _provider_failed(_: Request, exc: ProviderError) -> JSONResponse:
        _log.warning("provider 调用失败", error=str(exc))
        return JSONResponse(status_code=502, content={"detail": str(exc)})


app = create_app()
