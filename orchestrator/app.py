from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from db.session import dispose_db, init_db
from pool.manager import PoolManager
from services.sync_service import SyncService
from utils.config import settings
from utils.id import new_id
from utils.logging import (
    configure_logging,
    get_logger,
    log_event,
    reset_context,
    set_request_context,
)

from .background import cancel_background_tasks, scheduler_loop, track_bg_task
from .routes import accounts, data, pool as pool_routes, sync

configure_logging(settings.log_level, settings.log_json)
logger = get_logger(__name__)

TAGS = [
    {"name": "Accounts", "description": "Registered MT5 trading accounts."},
    {"name": "Sync", "description": "Queue a sync, or force one to the front of the queue."},
    {"name": "Data", "description": "Synced trading data, served from the database."},
    {"name": "Pool", "description": "Terminal pool health and queue depth."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    for problem in settings.problems():
        log_event(logger, "warning", "app.startup.config", problem=problem)

    paths = settings.terminals

    terminal_pool = PoolManager(
        paths,
        task_timeout_seconds=settings.sync_task_timeout_seconds,
        worker_start_timeout_seconds=settings.worker_start_timeout_seconds,
        max_task_retries=settings.max_task_retries,
        init_timeout_ms=settings.terminal_init_timeout_ms,
        login_timeout_ms=settings.terminal_login_timeout_ms,
        terminal_portable=settings.terminal_portable,
        history_settle_timeout_seconds=settings.history_settle_timeout_seconds,
        log_level=settings.log_level,
        log_json=settings.log_json,
        enrich_mae_mfe=settings.enrich_mae_mfe,
    )
    sync_service = SyncService(terminal_pool)
    terminal_pool.on_result = sync_service.persist

    app.state.pool = terminal_pool
    app.state.sync_service = sync_service

    # Every in-flight sync parks one thread in a blocking pipe read for its
    # whole duration - up to the task timeout. The default executor is
    # min(32, cpu_count + 4) threads and is shared with everything else, so it
    # is sized to the pool explicitly rather than left to chance.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=max(8, len(paths) * 2 + 8),
            thread_name_prefix="mt5-pipe",
        )
    )

    await terminal_pool.start()
    track_bg_task(asyncio.create_task(scheduler_loop(sync_service), name="sync-scheduler"))
    log_event(logger, "info", "app.startup.completed", terminals=len(paths))

    try:
        yield
    finally:
        await cancel_background_tasks()
        await terminal_pool.stop()
        await dispose_db()
        app.state.pool = None
        app.state.sync_service = None
        log_event(logger, "info", "app.shutdown.completed")


def create_app() -> FastAPI:
    app = FastAPI(
        title="MT5 Sync API",
        version="0.1.0",
        description=(
            "Syncs MT5 trading accounts into standardized FX models through a pool "
            "of MetaTrader 5 terminals."
        ),
        openapi_tags=TAGS,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.cors_allow_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        tokens = set_request_context(
            request_id=request.headers.get("x-request-id") or new_id()
        )
        try:
            return await call_next(request)
        finally:
            reset_context(tokens)

    for router in (accounts.router, sync.router, data.router, pool_routes.router):
        app.include_router(router)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    config = uvicorn.Config(app, host=settings.api_host, port=settings.api_port)
    server = uvicorn.Server(config)

    if sys.platform != "win32":
        server.run()
        return

    # psycopg refuses to run its async mode on a ProactorEventLoop, and uvicorn
    # hands itself one on Windows through an explicit loop_factory - setting the
    # event loop policy does not reach it. Drive the server on a selector loop.
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


if __name__ == "__main__":
    run()
