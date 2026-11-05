"""FastAPI 应用入口：实例构建、中间件、静态托管、生命周期。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes import router
from .config import AppConfig, load_config
from .llm import ClientRegistry
from .tasks import TaskRegistry

logger = logging.getLogger("img26")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# TTL 清理周期：每 5 分钟扫一次过期任务
CLEANUP_INTERVAL = 300


def create_app(config: AppConfig | None = None) -> FastAPI:
    """构建 FastAPI 应用（测试与运行共用）。"""
    cfg = config or load_config()
    if cfg.providers:
        logger.info(
            "已加载 %d 个 provider：%s",
            len(cfg.providers),
            ", ".join(p.name for p in cfg.providers),
        )
    else:
        logger.warning(
            "未配置任何 AI provider（config.yaml 缺失或 providers 为空）："
            "SVG convert 可用，generate 模式与 GIF 不可用"
        )

    app = FastAPI(
        title="GPT-Image-2.6 复刻（GIF + SVG）",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # CORS：本地 API 允许跨源调用（可配置）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.server.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.config = cfg
    app.state.registry = ClientRegistry(cfg)
    app.state.tasks = TaskRegistry(
        max_running=cfg.gif.max_concurrent_tasks,
        ttl_seconds=cfg.gif.task_ttl_seconds,
    )

    app.include_router(router)

    # 兜底不吞错：把未捕获异常统一为 500 JSON（FastAPI 默认会打印，但要确保响应一致）
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务器内部错误：{type(exc).__name__}: {exc}"},
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """周期清理过期任务的后台循环。"""
        app.state.cleanup_task = asyncio.create_task(_cleanup_loop())
        yield
        cleanup = getattr(app.state, "cleanup_task", None)
        if cleanup is not None:
            cleanup.cancel()

    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                removed = app.state.tasks.cleanup_expired()
                if removed:
                    logger.info("清理了 %d 个过期任务", removed)
            except Exception:  # noqa: BLE001 —— 清理失败只记录，不影响服务
                logger.exception("任务清理失败（忽略，下轮重试）")

    app.router.lifespan_context = lifespan

    # 静态前端托管（最后挂载，避免吞掉 /api 路由）
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
