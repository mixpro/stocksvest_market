"""Builds the app: shared context, each feature's routes, and the sample UI."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import Settings
from .deps import AppContext
from .automations import routes as automations_routes
from .automations.scheduler import AutomationScheduler
from .filters import routes as filters_routes
from .market import routes as market_routes
from .market.client import MarketAPI, MarketClient
from .runs import routes as runs_routes

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, market_factory: Callable[[Settings], MarketAPI] = MarketClient):
    settings = settings or Settings()
    ctx = AppContext(settings, market_factory(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(settings.db_path)
        db.init(conn)
        conn.close()
        if settings.run_scheduler:
            ctx.scheduler = AutomationScheduler(ctx)
            ctx.scheduler.start()
        yield
        if ctx.scheduler:
            ctx.scheduler.stop()

    app = FastAPI(
        title="StocksVest App",
        description="Filters and automations over the halal stock universe, backed by the StocksVest Market API.",
        version="1.2.0",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["status"])
    def health():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui/")

    app.include_router(filters_routes.router(ctx))
    app.include_router(automations_routes.router(ctx))
    app.include_router(runs_routes.router(ctx))
    app.include_router(market_routes.router(ctx))
    app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
