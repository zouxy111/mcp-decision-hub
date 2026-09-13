"""Application assembly: FastAPI + MCP sub-app + web routes + drive worker."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hub.api.accounts import seed_admin
from hub.api.errors import ApiError, error_payload
from hub.background import drive_worker, resume_worker, timeout_worker
from hub.config import Settings, load_settings
from hub.db.session import init_db, make_engine, make_session_factory
from hub.domain.rate_limit import RateLimiter
from hub.llm.client import DeepSeekClient
from hub.web import (
    routes_admin,
    routes_agent_rest,
    routes_agents,
    routes_api,
    routes_auth,
    routes_decision,
    routes_matters,
)

try:
    from hub.mcp_server.app import create_mcp_asgi
except ImportError:
    create_mcp_asgi = None


def _make_llm(settings: Settings):
    return DeepSeekClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_request_timeout_seconds,
    )


def _find_interrupted(session_factory) -> list[str]:
    from hub.api.pipeline import find_interrupted_round_ids

    with session_factory() as session:
        return find_interrupted_round_ids(session)


def _find_interrupted_resolutions(session_factory) -> list[str]:
    from hub.api.pipeline import find_interrupted_resolution_matter_ids

    with session_factory() as session:
        return find_interrupted_resolution_matter_ids(session)


def create_app(settings: Settings | None = None, *, llm=None) -> FastAPI:
    settings = settings or load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    if llm is None:
        llm = _make_llm(settings)
    drive_queue: asyncio.Queue[str] = asyncio.Queue()
    resume_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    mcp_asgi = None
    mcp_inner_lifespan = None
    limiter = RateLimiter()
    if create_mcp_asgi is not None:
        mcp_asgi, mcp_inner_lifespan = create_mcp_asgi(
            session_factory, settings, drive_queue, limiter=limiter,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with session_factory() as session:
            seed_admin(session, settings)
            session.commit()
        for round_id in await asyncio.to_thread(_find_interrupted, session_factory):
            drive_queue.put_nowait(round_id)
        for matter_id in await asyncio.to_thread(_find_interrupted_resolutions,
                                                 session_factory):
            resume_queue.put_nowait((matter_id, "decide"))
        worker = asyncio.create_task(
            drive_worker(drive_queue, session_factory, settings, llm)
        )
        gate_worker = asyncio.create_task(
            resume_worker(resume_queue, session_factory, settings, llm)
        )
        timeout_scan_task = asyncio.create_task(
            timeout_worker(session_factory, settings, drive_queue)
        )
        try:
            if mcp_inner_lifespan is not None:
                async with mcp_inner_lifespan(app):
                    yield
            else:
                yield
        finally:
            worker.cancel()
            gate_worker.cancel()
            timeout_scan_task.cancel()

    app = FastAPI(title="mcp-decision-hub", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.limiter = limiter
    app.state.llm = llm
    app.state.drive_queue = drive_queue
    app.state.resume_queue = resume_queue

    @app.exception_handler(ApiError)
    async def _api_error_handler(_request, exc: ApiError) -> JSONResponse:
        """统一错误形状 {error_code, message, details?}（PRD 9.5）。

        既有 HTML 路由均自行 try/except 捕获，故此处不影响其行为。
        """
        return JSONResponse(error_payload(exc), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request, exc: RequestValidationError):
        """JSON API 的载荷校验失败也走统一错误形状（422 而非 500）。

        仅作用于 /api/ 前缀；既有 HTML 路由沿用 FastAPI 默认处理，行为不变。
        """
        if request.url.path.startswith("/api/"):
            err = ApiError(422, "VALIDATION_FAILED", "请求载荷校验失败",
                           details={"errors": jsonable_encoder(exc.errors())})
            return JSONResponse(error_payload(err), status_code=err.status_code)
        return await request_validation_exception_handler(request, exc)

    app.include_router(routes_auth.router)
    app.include_router(routes_matters.router)
    app.include_router(routes_decision.router)
    app.include_router(routes_agents.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_api.router)
    app.include_router(routes_agent_rest.router)
    if mcp_asgi is not None:
        app.mount("/mcp", mcp_asgi)
    return app


app = create_app()
