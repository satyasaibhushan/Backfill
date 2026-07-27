from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backfill.api import providers, runs, system, tasks
from backfill.config import Settings, load_settings
from backfill.database import create_database_engine, initialize_database
from backfill.providers.service import ProviderService
from backfill.runner import AgentRunner
from backfill.scheduler import Scheduler


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)
        resolved_settings.resolved_worktree_dir.mkdir(parents=True, exist_ok=True)
        engine = create_database_engine(resolved_settings)
        initialize_database(engine)
        app.state.engine = engine
        app.state.settings = resolved_settings
        app.state.providers = ProviderService(resolved_settings, engine)
        app.state.runner = AgentRunner(resolved_settings, engine)
        app.state.scheduler = Scheduler(
            resolved_settings,
            engine,
            app.state.providers,
            app.state.runner,
        )
        await app.state.scheduler.start()
        yield
        await app.state.scheduler.stop()
        engine.dispose()

    app = FastAPI(
        title="Backfill",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(system.router)
    app.include_router(tasks.router)
    app.include_router(providers.router)
    app.include_router(runs.router)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")
    return app


app = create_app()
