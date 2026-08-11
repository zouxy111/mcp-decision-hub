"""Application assembly: FastAPI + MCP sub-app + web routes."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from hub.api.accounts import seed_admin
from hub.config import Settings, load_settings
from hub.db.session import init_db, make_engine, make_session_factory
from hub.web import routes_admin, routes_agents, routes_auth, routes_matters

try:
    from hub.mcp_server.app import create_mcp_asgi
except ImportError:  # MCP server module lands in task 18; web-only runs until then
    create_mcp_asgi = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    mcp_asgi = None
    mcp_inner_lifespan = None
    if create_mcp_asgi is not None:
        mcp_asgi, mcp_inner_lifespan = create_mcp_asgi(session_factory, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with session_factory() as session:
            seed_admin(session, settings)
            session.commit()
        if mcp_inner_lifespan is not None:
            async with mcp_inner_lifespan(app):
                yield
        else:
            yield

    app = FastAPI(title="mcp-decision-hub", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.include_router(routes_auth.router)
    app.include_router(routes_matters.router)
    app.include_router(routes_agents.router)
    app.include_router(routes_admin.router)
    if mcp_asgi is not None:
        app.mount("/mcp", mcp_asgi)
    return app


app = create_app()
